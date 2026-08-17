"""Running a job off the request thread, and saying so while it happens.

A thread rather than a queue: this host is one process against one SQLite
file, so a broker would add another thing to deploy and to fail. What replaces
its durability is the row — a job whose process vanished is `lost`, not
silently absent.

Two things a thread gets wrong are handled here rather than left to callers: a
database connection per thread, closed at the end, and every exception
recorded. An exception on a thread otherwise goes to stderr and the row sits
at `running` forever.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from core.audit import operation_context, record_event
from core.facets import Counts, Failure, Timing
from core.models import AuditLog

from .models import Job

# How often progress reaches the database, at most. Work may report per
# record; at a few million records the reporting would cost more than the work.
REPORT_SECONDS = 2.0


class Progress:
    """What the work reports through. Cheap to call, and rate-limited.

    The work gets this rather than the Job, so it can report progress but not
    change state: the runner owns what state means.
    """

    def __init__(self, job: Job):
        self._job = job
        self._last = 0.0
        self._note = ""

    def __call__(self, note: str = "", percent: int | None = None, *, force=False):
        now = timezone.now()
        # A changed note always writes. The limit is for a step repeating
        # itself, not for the work moving on: swallowing the transition
        # leaves the panel claiming to still be doing the previous step,
        # which is indistinguishable from a hang.
        changed = bool(note) and note != self._note
        if (
            not force
            and not changed
            and self._last
            and (now.timestamp() - self._last) < REPORT_SECONDS
        ):
            return
        if note:
            self._note = note
        self._last = now.timestamp()
        fields = {"heartbeat_at": now}
        if note:
            fields["note"] = note[:200]
        if percent is not None:
            fields["percent"] = max(0, min(100, int(percent)))
        # `update`, not `save`: this runs while the work holds its own
        # transactions, and a full save would write columns it is changing.
        # It also cannot resurrect a job something else has finished.
        Job.objects.filter(pk=self._job.pk).update(**fields)


class JobConflict(RuntimeError):
    """A live job of this kind already exists."""


def start(
    kind: str,
    label: str,
    work: Callable[[Progress], dict[str, Any]],
    *,
    actor: str = "",
    requested_by=None,
    request: dict | None = None,
) -> Job:
    """Record the job, then run it on a thread. Returns once recorded.

    `work` is called with a `Progress`; whatever it returns is stored as the
    job's result.
    """
    try:
        with transaction.atomic():
            job = Job.objects.create(
                kind=kind,
                label=label,
                actor=actor,
                requested_by=requested_by,
                request=request or {},
                state=Job.State.QUEUED,
            )
    except Exception as exc:  # unique constraint: one live job per kind
        raise JobConflict(f"A {kind} job is already running.") from exc

    thread = threading.Thread(
        target=_run, args=(job.pk, work), name=f"job:{kind}", daemon=True
    )
    thread.start()
    return job


def _run(job_id, work: Callable[[Progress], dict[str, Any]]) -> None:
    close_old_connections()
    started = timezone.now()
    Job.objects.filter(pk=job_id).update(
        state=Job.State.RUNNING, started_at=started, heartbeat_at=started
    )
    job = Job.objects.get(pk=job_id)
    progress = Progress(job)

    # One context around everything, entered once, with the job id as the
    # operation id — so "what did this import change?" is a query.
    #
    # Around the whole try/except, not inside each branch: a
    # `@contextmanager` is a single-use generator, and a second `with` would
    # raise on the failure path.
    with operation_context(
        interface="job",
        actor=job.actor or "local-operator",
        operation=job.kind,
        operation_id=str(job.pk),
    ):
        # The audit row is written before the state changes, both ways round:
        # anything that sees a job finished can rely on the record of how it
        # finished already existing. The other order is a race that hides,
        # since the audit lands microseconds later.
        try:
            result = work(progress) or {}
            ended = timezone.now()
            # Counts come from the work, which is the only thing that knows
            # what it changed. Reported as a facet so `counts.created` means
            # the same here as it does for an infrastructure reconcile.
            _audit(
                job,
                AuditLog.Action.IMPORTED,
                "finished",
                result,
                ended,
                facets=_counts(result),
            )
            Job.objects.filter(pk=job_id).update(
                state=Job.State.SUCCEEDED,
                result=result,
                percent=100,
                finished_at=ended,
                heartbeat_at=ended,
            )
        except Exception:
            # The whole traceback, not just the message: nobody can reproduce
            # a background failure by running it again from a terminal.
            detail = traceback.format_exc()
            ended = timezone.now()
            _audit(
                job,
                AuditLog.Action.FAILED,
                "failed",
                {},
                ended,
                facets=(
                    Failure(
                        message=detail.strip().splitlines()[-1][:400],
                        kind=detail.strip().splitlines()[-1].split(":")[0][:80],
                    ),
                ),
            )
            Job.objects.filter(pk=job_id).update(
                state=Job.State.FAILED,
                error=detail[-8000:],
                finished_at=ended,
                heartbeat_at=ended,
            )
        finally:
            connection.close()



def _counts(result: dict):
    """The work's own numbers, where it reported any in the shared shape.

    Read rather than required: work returns whatever it wants, and a job that
    reports nothing countable is normal. What is not normal is inventing a
    count that was never measured, so anything absent stays absent.
    """
    counts = Counts(
        seen=result.get("seen"),
        created=result.get("created"),
        updated=result.get("updated"),
        skipped=result.get("skipped"),
    )
    return (counts,) if counts.as_metadata() else ()


def _audit(job: Job, action: str, outcome: str, detail: dict, ended, facets=()) -> None:
    """Record how the job ended, attributed to whoever asked for it.

    The user is passed explicitly: `record_event` otherwise reads the current
    request's user, and there is no request on a worker thread. Creation is
    audited by the model signal; without this the log would show every job
    starting and none ending.
    """
    # Measured from the in-memory job's start: the row is not marked finished
    # until after this returns.
    seconds = (ended - job.started_at).total_seconds() if job.started_at else 0
    took = f" in {seconds:.0f}s" if seconds >= 1 else ""
    record_event(
        action=action,
        obj=job,
        type_label="Background job",
        message=f"{job.label} {outcome}{took}",
        # Timing as a facet rather than baked into the message, so how long a
        # job takes is a number that can be compared with the last one rather
        # than a phrase that has to be read.
        facets=(Timing(duration_ms=round(seconds * 1000)), *facets),
        metadata={"kind": job.kind, "job": str(job.pk), **detail},
        user=job.requested_by,
    )


def reap(kind: str | None = None) -> int:
    """Mark jobs whose process stopped without finishing.

    Called from the pages that list jobs rather than from a scheduler: a stale
    job only blocks the next job of its kind, so the moment somebody looks is
    when the answer is needed.
    """
    live = Job.objects.filter(state=Job.State.RUNNING).select_related("requested_by")
    if kind:
        live = live.filter(kind=kind)
    lost = [job for job in live if job.is_stale]
    if not lost:
        return 0
    ended = timezone.now()
    # Audited like any other ending: nothing was watching when this died, so
    # this is the only record of it there will be.
    for job in lost:
        _audit(
            job,
            AuditLog.Action.FAILED,
            "was lost",
            {"last_said": job.note, "last_beat": job.heartbeat_at.isoformat()},
            ended,
        )
    return Job.objects.filter(pk__in=[job.pk for job in lost]).update(
        state=Job.State.LOST,
        error="The process running this job stopped without finishing it.",
        finished_at=ended,
    )
