"""The runs of HQ's playbooks and the scans of their source as a readable
resource, declared beside Jobs.

Each run says what Jobs and its own page show of it to anyone who may read
it, and nothing more.
"""

import uuid
from datetime import timedelta
from typing import Annotated, Any, Literal, TypedDict

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone
from pydantic import Field, create_model

from hq.domains.jobs.models import Job, JobState

from .automation_manifest import DottedName
from .automation_runs import CORRELATION_LONGEST, CORRELATION_PATTERN
from .command_fields import RequiredText
from .describe import Describe, listed
from .integration_specs import ListFilter, ListRead, ListSort, ResourceSpec
from .managed_records import AskedAt
from .projection import iso, page_of
from .record_errors import RecordNotFound
from .resources import BoundedQuery, SearchText, one_of
from .security import Capability
from .tone import Tone
from .ui import ended
from .work_runs import (
    WorkRun,
    hydrated,
    listed_runs,
    on_hosts,
    operation_of,
    run_hosts,
    run_of,
    run_rows,
    work,
    work_kinds,
)

RUNS = "runs"
# Names sent comma-separated: a kind of work, or a machine.
_NAMES = r"^[A-Za-z0-9_.:-]{1,253}(?:,[A-Za-z0-9_.:-]{1,253})*$"


class RunTask(TypedDict):
    """One task's outcome on one machine, as the worker reported it."""

    host: Annotated[str, Describe("The machine the task ran on, as the playbook's inventory names it.")]
    task: Annotated[str, Describe("The task, by its name in the playbook.")]
    outcome: Annotated[str, Describe("How it ended: `ok`, `changed`, `skipped`, `failed` or `unreachable`.")]
    message: Annotated[str, Describe("What the task said; empty when it said nothing.")]
    tone: Annotated[str, Describe(ended(f"How its outcome is colored: {listed(Tone)}"))]


class ServedPart(TypedDict):
    """One name a certificate check found serving another certificate."""

    consumer: Annotated[str, Describe("What serves the name, as the check names it.")]
    domain: Annotated[str, Describe("The name it serves.")]
    fingerprint_sha256: Annotated[str, Describe("The SHA-256 fingerprint of the certificate it serves.")]


class _Work(TypedDict):
    """What a piece of work says of itself, a job or a run."""

    url: Annotated[str, Describe("Its page in HQ; empty for a job, which has none.")]
    title: Annotated[str, Describe("What it is, in words.")]
    state: Annotated[
        str,
        Describe(
            ended(f"Where it stands, as Jobs shows it: {listed(JobState.values)}") + " "
            f"`{JobState.LOST}` is work whose worker stopped reporting."
        ),
    ]
    state_label: Annotated[str, Describe("That state in the words HQ's pages say it in.")]
    tone: Annotated[str, Describe(ended(f"How that state is colored: {listed(Tone)}"))]
    headline: Annotated[str, Describe("What it is doing or how it ended, in a sentence.")]
    hosts: Annotated[list[str], Describe("The machines it acts on, each by its name in HQ.")]
    by: Annotated[str, Describe("Who asked for it, through which door, and who approved it, as Jobs names them.")]
    approved_by: Annotated[str, Describe("Who approved it; empty for work that needed no approval.")]
    correlation: Annotated[str, Describe("The correlation id its asker sent; empty when it sent none.")]
    created_at: AskedAt
    started_at: Annotated[str | None, Describe("When a worker took it up; null while it waits.")]
    finished_at: Annotated[str | None, Describe("When it ended; null while it waits or works.")]
    duration_seconds: Annotated[float | None, Describe("How long it worked for; null until it ends.")]
    stated: Annotated[
        list[str], Describe("The kinds of reading its playbook stated, each once; empty when it stated none.")
    ]


class _Evidence(TypedDict):
    """What a run shows for itself past its row: the worker's lease while it holds it, and each task's outcome."""

    effect: Annotated[
        str, Describe("What its playbook changes, as its record pins it, which cancelling it takes the authority of.")
    ]
    attempts: Annotated[int, Describe("How many times a worker has taken it up.")]
    worker: Annotated[str, Describe("The worker running it now; empty unless one is.")]
    lease_until: Annotated[
        str | None,
        Describe("When that worker must next report, or HQ reads the run as stopped; null unless one runs it."),
    ]
    events: Annotated[list[RunTask], Describe("Each task's outcome on each machine, in the order the worker ran them.")]
    expected: Annotated[
        str,
        Describe("The SHA-256 fingerprint of the certificate it checked each name for; empty when it checked none."),
    ]
    affected: Annotated[list[ServedPart], Describe("Each name it found serving another certificate.")]
    matched: Annotated[int, Describe("How many names it found serving the certificate it checked for.")]


class RunRow(_Work):
    operation_id: Annotated[
        str, Describe("The run's id: the `operation.id` its command answered, which a read of one names.")
    ]
    playbook: Annotated[
        str, Describe("The id of the playbook it runs, which the `playbook` filter takes; empty for a scan.")
    ]
    capability: Annotated[
        str, Describe("The command it was asked for through, by name: a playbook's, or the scan's refresh.")
    ]


class RunRecord(RunRow, _Evidence):
    """One run whole: its row and its evidence."""

    reason: Annotated[str, Describe("Why its asker asked for it; empty when they gave no reason.")]


class WorkItem(_Work, _Evidence):
    """One job or run as Jobs lists it, its evidence beneath."""

    id: Annotated[str, Describe("Its id: a run's `operation.id`, or a job's.")]
    source: Annotated[Literal["job", "run"], Describe("Whether it is one of HQ's own jobs or a run of automation.")]
    kind: Annotated[
        str, Describe("What the `kind` filter names it by: a job's kind, or the command a run was asked through.")
    ]


RunQuery = create_model(
    "RunQuery",
    __base__=BoundedQuery,
    playbook=(
        DottedName | None,
        Field(
            default=None, max_length=200, description="Only the runs of the playbook with this id; absent, every one."
        ),
    ),
    machine=(
        RequiredText | None,
        Field(
            default=None,
            max_length=253,
            description="Only the runs on the machine with this name, as `machines` names it; absent, every machine.",
        ),
    ),
    correlation=(
        RequiredText | None,
        Field(
            default=None,
            max_length=CORRELATION_LONGEST,
            pattern=CORRELATION_PATTERN,
            description="Only the runs asked for with this correlation id; absent, every run.",
        ),
    ),
    state=(
        one_of(JobState.values) | None,
        Field(default=None, description="Only the runs in this state, as Jobs shows it; absent, every state."),
    ),
)

# Newest first, the order Jobs lists work in.
_ORDER = ("-created_at",)


def _work(run: WorkRun) -> dict[str, Any]:
    return {
        "url": run.url,
        "title": run.title,
        "state": run.state,
        "state_label": run.word,
        "tone": run.tone,
        "headline": run.note,
        "hosts": list(run.evidence.hosts),
        "by": run.by.words,
        "approved_by": run.by.approved_by,
        "correlation": run.evidence.correlation,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started),
        "finished_at": iso(run.finished),
        # Only an ended run's, so two reads of the same run answer alike.
        "duration_seconds": run.duration if run.finished else None,
        "stated": list(run.evidence.stated),
    }


def _row(run: WorkRun) -> dict[str, Any]:
    return {
        **_work(run),
        "operation_id": run.id,
        "playbook": operation_of(run.kind),
        "capability": run.kind,
    }


def _evidence(run: WorkRun) -> dict[str, Any]:
    evidence = run.evidence
    return {
        "effect": run.effect,
        "attempts": evidence.attempts,
        "worker": evidence.worker,
        "lease_until": iso(evidence.lease_until),
        # The fields a task's line on Jobs draws, whatever else the report held.
        "events": [{name: str(event.get(name) or "") for name in RunTask.__annotations__} for event in evidence.events],
        "expected": evidence.expected,
        "affected": [
            {name: str(item.get(name) or "") for name in ServedPart.__annotations__} for item in evidence.affected
        ],
        "matched": evidence.matched,
    }


def _item(run: WorkRun) -> dict[str, Any]:
    return {
        **_work(run),
        **_evidence(run),
        "id": run.id,
        "source": run.source,
        "kind": run.kind,
    }


def list_runs(
    *,
    playbook: str | None = None,
    machine: str | None = None,
    correlation: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """The runs that match, newest first, a page at a time."""

    found = listed_runs(
        playbook=playbook or "", host=machine or "", correlation=correlation or "", state=(state,) if state else ()
    )
    page = page_of(found, limit, order=_ORDER)
    return {**page, "items": [_row(run) for run in run_rows(page["items"])]}


def get_run(run_id: str) -> dict[str, Any]:
    """One run or scan by its operation id."""

    try:
        found = run_of(str(uuid.UUID(run_id)))
    except ValueError, DjangoValidationError:
        found = None
    if found is None:
        raise RecordNotFound("No run of a playbook or scan of its source has that id.")
    run, operation = found
    return {**_row(run), **_evidence(run), "reason": operation.reason}


def list_work(*, query: str | None = None, limit: int = 50) -> dict[str, Any]:
    """HQ's jobs and the automation runs it queued, as one list, a page at a time."""

    page = page_of(work(query or ""), limit)
    return {**page, "items": [_item(run) for run in hydrated(page["items"])]}


def get_work(work_id: str) -> dict[str, Any]:
    """One job or run by its id, as the list says it."""

    try:
        found = hydrated(work().kept(Q(pk=uuid.UUID(work_id))))
    except ValueError:
        found = []
    if not found:
        raise RecordNotFound("No job or run has that id.")
    return _item(found[0])


class WorkQuery(BoundedQuery):
    query: SearchText = None


def _jobs(*made: tuple[str, str, JobState, int]) -> list[str]:
    """Jobs of each kind, label, state and age in hours, as their ids."""

    now, ids = timezone.now(), []
    for kind, label, state, hours in made:
        job = Job.objects.create(kind=kind, label=label, state=state)
        # Dated after the insert: the column stamps itself on create.
        Job.objects.filter(pk=job.pk).update(created_at=now - timedelta(hours=hours))
        ids.append(str(job.pk))
    return ids


def _failed_first() -> tuple[str, ...]:
    """Work whose kind's latest run failed leads, then the newest first: a
    kind that failed and ran again since leads no more."""

    recovered, again, failed, newest = _jobs(
        ("example.recovers", "Recovers", JobState.FAILED, 5),
        ("example.recovers", "Recovers", JobState.SUCCEEDED, 1),
        ("example.fails", "Fails", JobState.FAILED, 3),
        ("example.newest", "Newest", JobState.SUCCEEDED, 0),
    )
    return failed, newest, again, recovered


def _a_to_z() -> tuple[str, ...]:
    """What work is called, read case-blind."""

    zebra, alpha = _jobs(
        ("example.zebra", "Zebra work", JobState.SUCCEEDED, 0), ("example.alpha", "alpha work", JobState.SUCCEEDED, 0)
    )
    return alpha, zebra


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "work",
            "Background jobs",
            "HQ's own jobs and the automation runs it queued, as Jobs lists them: who asked, its state, its "
            "machines and each task's outcome.",
            Capability.READ,
            listing=ListRead(
                list_work,
                WorkQuery,
                WorkItem,
                sorts=(
                    ListSort("failed", "Failed first", ("lead", "-created_at", "-id"), _failed_first),
                    ListSort("-created_at", "Newest first", ("-created_at", "-id")),
                    ListSort("created_at", "Oldest first", ("created_at", "id")),
                    ListSort("title", "Job A–Z", ("title", "-created_at", "-id"), _a_to_z),
                    ListSort("state", "State", ("seen", "-created_at", "-id")),
                ),
                filters=(
                    ListFilter("state", "State", JobState.choices, lookup="seen"),
                    # A kind and a machine are offered where work has named them.
                    ListFilter("kind", "Job", lambda: [(k.kind, k.title) for k in work_kinds()], pattern=_NAMES),
                    ListFilter(
                        "host", "Host", lambda: [(host, host) for host in run_hosts()], pattern=_NAMES, narrows=on_hosts
                    ),
                ),
                counted=False,
            ),
            detail_handler=get_work,
            identifier="id",
            web_route="jobs:list",
            record_type=WorkItem,
        ),
        ResourceSpec(
            RUNS,
            "Playbook runs",
            "Every run of a playbook, and every scan of their source: its machines, who asked and approved it, "
            "its state and each task's outcome.",
            Capability.READ,
            listing=ListRead(list_runs, RunQuery, RunRow),
            detail_handler=get_run,
            identifier="operation_id",
            web_route="jobs:list",
            record_type=RunRecord,
        ),
    )
