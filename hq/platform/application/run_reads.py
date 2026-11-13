"""The runs of HQ's playbooks as a readable resource, declared beside Jobs.

An agent or a client that asked for a run reads it here by the operation id
its answer carried, or lists the runs of a playbook, a machine or its own
correlation id. Each run says what Jobs shows of it to anyone who may read
it, and nothing more: its machines, who asked and who agreed to it, the
worker holding it, and each task's host, name, outcome and message.
"""

import uuid
from typing import Annotated, Any, TypedDict

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import Field, create_model

from hq.domains.jobs.models import JobState

from .automation_manifest import DottedName
from .automation_runs import CORRELATION_LONGEST, CORRELATION_PATTERN, capability_name
from .describe import Describe, listed
from .integration_specs import ResourceSpec
from .projection import iso, keyset_page
from .record_errors import RecordNotFound
from .resource_records import AskedAt
from .resources import BoundedQuery, one_of
from .security import Capability
from .ui import ended
from .work_runs import WorkRun, operation_of, playbook_runs, run_row

RUNS = "runs"


class RunTask(TypedDict):
    """One task's outcome on one machine, as the worker reported it."""

    host: Annotated[str, Describe("The machine the task ran on, as the playbook's inventory names it.")]
    task: Annotated[str, Describe("The task, by its name in the playbook.")]
    outcome: Annotated[str, Describe("How it ended: `ok`, `changed`, `skipped`, `failed` or `unreachable`.")]
    message: Annotated[str, Describe("What the task said; empty when it said nothing.")]


class RunRow(TypedDict):
    operation_id: Annotated[
        str, Describe("The run's id: the `operation.id` its command answered, which a read of one names.")
    ]
    playbook: Annotated[str, Describe("The id of the playbook it runs, which the `playbook` filter takes.")]
    capability: Annotated[str, Describe("The command it was asked for through, by name.")]
    state: Annotated[
        str,
        Describe(
            ended(f"Where it stands, as Jobs shows it: {listed(JobState.values)}") + " "
            f"`{JobState.LOST}` is a run whose worker stopped reporting."
        ),
    ]
    state_label: Annotated[str, Describe("That state in the words HQ's pages say it in.")]
    headline: Annotated[str, Describe("What it is doing or how it ended, in a sentence.")]
    hosts: Annotated[list[str], Describe("The machines it acts on, each by its name in HQ.")]
    by: Annotated[str, Describe("Who asked for it, as Jobs names them.")]
    approved_by: Annotated[str, Describe("Who approved it; empty for a run that needed no approval.")]
    correlation: Annotated[str, Describe("The correlation id its asker sent; empty when it sent none.")]
    created_at: AskedAt
    started_at: Annotated[str | None, Describe("When a worker took it up; null while it waits.")]
    finished_at: Annotated[str | None, Describe("When it ended; null while it waits or works.")]
    duration_seconds: Annotated[float | None, Describe("How long it worked for; null until it ends.")]


class RunRecord(RunRow):
    """One run whole: its row, the worker's lease while it holds it, and each task's outcome."""

    worker: Annotated[str, Describe("The worker running it now; empty unless one is.")]
    lease_until: Annotated[
        str | None,
        Describe("When that worker must next report, or HQ reads the run as stopped; null unless one runs it."),
    ]
    events: Annotated[list[RunTask], Describe("Each task's outcome on each machine, in the order the worker ran them.")]


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
        str | None,
        Field(
            default=None,
            min_length=1,
            max_length=253,
            description="Only the runs on the machine with this name, as `machines` names it; absent, every machine.",
        ),
    ),
    correlation=(
        str | None,
        Field(
            default=None,
            min_length=1,
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


def _row(run: WorkRun) -> dict[str, Any]:
    playbook = operation_of(run.kind)
    return {
        "operation_id": run.id,
        "playbook": playbook,
        "capability": capability_name(playbook),
        "state": run.state,
        "state_label": run.word,
        "headline": run.note,
        "hosts": list(run.evidence.hosts),
        "by": run.by,
        "approved_by": run.evidence.approved_by,
        "correlation": run.evidence.correlation,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started),
        "finished_at": iso(run.finished),
        # Only an ended run's, so two reads of the same run answer alike.
        "duration_seconds": run.duration if run.finished else None,
    }


def _record(run: WorkRun) -> dict[str, Any]:
    evidence = run.evidence
    return {
        **_row(run),
        "worker": evidence.worker,
        "lease_until": iso(evidence.lease_until),
        # The four fields a task's line on Jobs draws, whatever else the report held.
        "events": [{name: str(event.get(name) or "") for name in RunTask.__annotations__} for event in evidence.events],
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

    found = playbook_runs(
        playbook=playbook or "", host=machine or "", correlation=correlation or "", state=(state,) if state else ()
    )
    page = keyset_page(found, _ORDER, limit)
    return {**page, "items": [_row(run_row(operation)) for operation in page["items"]]}


def get_run(run_id: str) -> dict[str, Any]:
    """One run by its operation id."""

    try:
        operation = playbook_runs().filter(pk=uuid.UUID(run_id)).first()
    except ValueError, DjangoValidationError:
        operation = None
    if operation is None:
        raise RecordNotFound("No run of a playbook has that id.")
    return _record(run_row(operation))


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            RUNS,
            "Playbook runs",
            "Every run of a playbook: its machines, who asked and approved it, its state and each task's outcome.",
            Capability.READ,
            list_runs,
            RunQuery,
            get_run,
            "operation_id",
            web_route="jobs:list",
            row_type=RunRow,
            record_type=RunRecord,
        ),
    )
