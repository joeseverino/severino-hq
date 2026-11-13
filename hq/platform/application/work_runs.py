"""All the work HQ started, as one list: its own jobs and the automation runs
it queued, each said in the same words and drawn in the same row shape.

A job stays a ``jobs.Job`` and a run an ``OperationRequest``, since the
runner's lock and the worker's claim and lease need each record as it is. The
list is a database union of the two, filtered, ordered and paginated there,
whose page of ids is then read back with one query per source, so a page
costs the same however much work has run. A held run is an
``ApprovalRequest``, not an operation, and is not listed here.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from django.db.models import Case, CharField, F, Q, QuerySet, Value, When
from django.db.models.fields.json import KT
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest, OperationState
from hq.domains.jobs.models import HEARTBEAT_GRACE, Job, JobState

from .asks import job_standing
from .automation_runs import capability_name, resource_key, run_title
from .resource_operations import operation_summary
lazy from .scheduled_work import job_names

type Source = Literal["job", "run"]
JOB: Source = "job"
RUN: Source = "run"

# Each state's tone, the same for a job and a run, in the vocabulary
# ``resource_operations.operation_summary`` gives an operation's.
TONES = {
    JobState.QUEUED: "paused",
    JobState.RUNNING: "active",
    JobState.SUCCEEDED: "good",
    JobState.FAILED: "serious",
    # Stopped early: a process or a worker that vanished, a restart to look at
    # rather than a fault to read.
    JobState.LOST: "attention",
}
# What a claimed run whose lease ran out says until the next claim ends it.
LAPSED = "Its worker stopped renewing its lease, so whether it finished is not known."

# The orders the list can be read in, each with a stable tie-break.
NEWEST, OLDEST, BY_STATE = "-created_at", "created_at", "state"
ORDERS = {
    NEWEST: ("-created_at", "-id"),
    OLDEST: ("created_at", "id"),
    BY_STATE: ("seen", "-created_at", "-id"),
}
# How many runs a machine's or an operation's page shows.
RECENT = 10

# A run's procedure is named on Jobs by the command it is run through.
_RUN_KIND = ("automation.", ".run")


@dataclass(frozen=True, slots=True)
class WorkRun:
    """One piece of work in the row every list of work draws."""

    source: Source
    id: str
    # What it is in words, and the name the list filters it by: a job's kind,
    # or the command a run's procedure is run through.
    title: str
    kind: str
    # Its state as seen (``JobState``'s value), in words and as a tone.
    state: str
    word: str
    tone: str
    # What it is doing or how it ended, in a sentence.
    note: str
    by: str
    approved_by: str
    created_at: datetime
    started: datetime | None
    finished: datetime | None
    duration: float | None
    # A run's machines, its asker's correlation id and its tasks' outcomes;
    # empty for a job.
    hosts: tuple[str, ...] = ()
    correlation: str = ""
    events: tuple[dict[str, Any], ...] = ()


def job_seen_state(now: datetime) -> Case:
    """``Job.seen_state`` as SQL, so a list filters and orders by what it shows."""

    stale = now - HEARTBEAT_GRACE
    return Case(
        When(Q(state=JobState.RUNNING, heartbeat_at__isnull=False, heartbeat_at__lt=stale), then=Value(JobState.LOST)),
        When(Q(state=JobState.QUEUED, created_at__lt=stale), then=Value(JobState.LOST)),
        default=F("state"),
        output_field=CharField(),
    )


def run_seen_state(now: datetime) -> Case:
    """A run's state as a job's is said (``OperationState.as_job``), where a
    claimed run whose lease has run out has stopped early whether or not the
    next claim has ended it yet, as a job nothing has heard from has."""

    lapsed = Q(state=OperationState.CLAIMED) & (Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
    return Case(
        When(lapsed, then=Value(JobState.LOST)),
        *(When(state=state, then=Value(state.as_job.value)) for state in OperationState),
        output_field=CharField(),
    )


def operation_of(kind: str) -> str:
    """The procedure a run kind names, or "" when it names none."""

    start, end = _RUN_KIND
    return kind[len(start) : -len(end)] if kind.startswith(start) and kind.endswith(end) else ""


def _rows(rows: QuerySet, source: Source) -> QuerySet:
    """One source's side of the union, unordered as a compound statement's parts must be."""

    return (
        rows.annotate(source=Value(source, output_field=CharField()))
        .values_list("source", "id", "seen", "created_at")
        .order_by()
    )


def _jobs(now: datetime, *, state: str, kind: str, host: str, search: str) -> QuerySet:
    jobs = Job.objects.annotate(seen=job_seen_state(now))
    if host:
        # A job acts on no machine.
        return _rows(jobs.none(), JOB)
    if state:
        jobs = jobs.filter(seen=state)
    if kind:
        jobs = jobs.filter(kind=kind)
    if search:
        jobs = jobs.filter(Q(label__icontains=search) | Q(note__icontains=search))
    return _rows(jobs, JOB)


def _runs(now: datetime, *, state: str, kind: str, host: str, search: str) -> QuerySet:
    runs = OperationRequest.objects.filter(action=OperationRequest.Action.RUN).annotate(seen=run_seen_state(now))
    if state:
        runs = runs.filter(seen=state)
    if kind:
        procedure = operation_of(kind)
        if not procedure:
            return _rows(runs.none(), RUN)
        runs = runs.filter(resource__key=resource_key(procedure))
    if host:
        runs = runs.filter(hosts__host=host)
    if search:
        runs = runs.annotate(operation=KT("input__operation"), said=KT("result__message")).filter(
            Q(operation__icontains=search) | Q(said__icontains=search)
        )
    return _rows(runs, RUN)


def work_rows(
    *,
    state: str = "",
    kind: str = "",
    host: str = "",
    search: str = "",
    order: str = NEWEST,
    now: datetime | None = None,
) -> QuerySet:
    """Every job and run that matches, as ``(source, id, seen, created_at)``
    rows in ``order``: a queryset a paginator counts and slices in the database.

    ``state`` is a ``JobState`` value as seen, ``kind`` a job's kind or a run's
    command name, ``host`` a machine's canonical name (a job acts on none).
    ``hydrated`` turns a page of it into ``WorkRun`` rows.
    """

    now = now or timezone.now()
    asked = {"state": state, "kind": kind, "host": host, "search": search}
    return _jobs(now, **asked).union(_runs(now, **asked), all=True).order_by(*ORDERS[order])


def hydrated(rows: Iterable[Sequence[Any]]) -> list[WorkRun]:
    """A page of ``work_rows`` as ``WorkRun`` rows, in its order, read with one query per source."""

    page = list(rows)
    ids: dict[str, list[Any]] = {JOB: [], RUN: []}
    for source, pk, _seen, _created in page:
        ids[source].append(pk)
    jobs = Job.objects.select_related("requested_by").in_bulk(ids[JOB]) if ids[JOB] else {}
    runs = OperationRequest.objects.select_related("resource").in_bulk(ids[RUN]) if ids[RUN] else {}
    names = job_names() if jobs else {}
    now = timezone.now()
    built: list[WorkRun] = []
    for source, pk, seen, _created in page:
        if source == JOB:
            built.append(_job_row(jobs[pk], names))
        else:
            built.append(_run_row(runs[pk], JobState(seen), now))
    return built


def _job_row(job: Job, names: dict[str, str]) -> WorkRun:
    standing = job_standing(job)
    return WorkRun(
        source=JOB,
        id=str(job.pk),
        title=names.get(job.kind) or job.label or job.kind,
        kind=job.kind,
        state=job.seen_state,
        word=standing.label,
        tone=TONES[JobState(job.seen_state)],
        note=standing.note,
        by=job.requested_by.get_username() if job.requested_by else "",
        approved_by="",
        created_at=job.created_at,
        started=job.started_at,
        finished=job.finished_at,
        duration=job.duration_seconds,
    )


def _run_row(operation: OperationRequest, seen: JobState, now: datetime) -> WorkRun:
    summary = operation_summary(operation)
    asked = operation.input or {}
    procedure = str(asked.get("operation") or "")
    lapsed = seen == JobState.LOST and operation.state == OperationState.CLAIMED
    started, finished = operation.claimed_at, operation.completed_at
    return WorkRun(
        source=RUN,
        id=str(operation.pk),
        title=run_title(procedure),
        kind=capability_name(procedure),
        state=seen.value,
        word=seen.label,
        # A run that finished and found a problem says so in its tone.
        tone=summary["tone"] if seen == JobState.SUCCEEDED else TONES[seen],
        note=LAPSED if lapsed else summary["headline"],
        by=summary["by"],
        approved_by=summary["approved_by"],
        created_at=operation.created_at,
        started=started,
        finished=finished,
        # A lapsed run was last heard from when its lease ran out.
        duration=_seconds(started, finished or (operation.lease_expires_at if lapsed else now)),
        hosts=tuple(asked.get("hosts") or ()),
        correlation=str(asked.get("correlation") or ""),
        events=tuple((operation.result or {}).get("events") or ()),
    )


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(timedelta(0), end - start).total_seconds()


def runs_on(host: str, limit: int = RECENT) -> list[WorkRun]:
    """The latest runs that acted on the machine ``host``, newest first."""

    return hydrated(work_rows(host=host)[:limit])


def recent_runs(operation_id: str, limit: int = RECENT) -> list[WorkRun]:
    """The latest runs of the procedure ``operation_id``, newest first."""

    return hydrated(work_rows(kind=capability_name(operation_id))[:limit])
