"""A call as a task of the MCP tasks extension, which a client waits on while a person decides it or HQ's work runs.

A task is the record its call made: the hold, by its id, for a call held for
a person, or the operation it queued, by its id, for a call that queued work.
Its state is read from that record at each ``tasks/get``. A hold's task goes
on to follow the operation its approved call queued, so a run held for
approval is one task across the hold and the run. ``tasks/cancel`` runs the
command the asker would: ``approval.withdraw`` while no person has decided,
``automation.run.cancel`` while HQ's runner has not started the run.
docs/API.md states the properties.
"""

from collections.abc import Callable, Mapping
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from mcp import types
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application import approval_review, approvals
from hq.platform.application.capabilities import run_capability
from hq.platform.application.held_answer import applying
from hq.platform.application.held_reads import ran, standing
from hq.platform.application.refusal_status import refusal_of
from hq.platform.application.security import Principal
from hq.platform.application.ui import ended
lazy from hq.platform.application.automation_runs import CANCEL
lazy from hq.platform.application.projection import iso
lazy from hq.platform.application.resource_operations import operation_asked
lazy from hq.platform.application.work_runs import UNENDED, run_row, standing_of

IDENTIFIER = "io.modelcontextprotocol/tasks"
# A person answers in minutes or hours, so a poll sooner than this only reads
# the same hold again. It is the longest interval Claude Code honors.
POLL_INTERVAL_MS = 30_000
# Work under way ends in seconds or minutes, so it is read again soon.
RUNNING_POLL_INTERVAL_MS = 2_000

# The field of an answer that carries the work its call queued (``OperationAsked``).
_QUEUED = "operation"

# The one answer for an id this caller did not ask for, whether or not it exists.
_UNKNOWN = "No task of this caller has that id."

type Record = ApprovalRequest | OperationRequest


class UpdateTaskRequestParams(types.GetTaskRequestParams):
    """``tasks/update``: answers to a task's input requests, which no task of HQ's makes."""

    input_responses: dict[str, Any] = Field(default_factory=dict)


def requested(context: Any) -> bool:
    """Whether this request's client takes a task in place of the call's answer.

    The extension's result is 2026-07-28 vocabulary, so only a client on that
    wire that declared it on this request is given one.
    """

    if context is None or context.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return False
    declared = context.session.client_capabilities
    return bool(declared and declared.extensions and IDENTIFIER in declared.extensions)


def follows(answer: object) -> bool:
    """Whether a tool's answer is one a task can follow: a call held for a person, or work it queued."""

    return isinstance(answer, dict) and (approvals.is_held(answer) or _queued(answer) is not None)


def created(answer: dict[str, Any], principal: Principal) -> dict[str, Any] | None:
    """The task for a call's answer, or None where the call keeps its answer.

    A repeat of another caller's request is answered with that request, which
    this caller could not read as a task, and work that already ended leaves
    nothing to wait on.
    """

    if approvals.is_held(answer):
        record: Record | None = approvals.own_held_request(str(answer["approval"]["id"]), principal=principal)
    else:
        record = _own_operation(_queued(answer) or "", principal)
        if record is not None and record.state not in UNENDED:
            record = None
    if record is None:
        return None
    task, _ = _task(record)
    return {"resultType": "task", **task}


def state(task_id: str, principal: Principal) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The task's fields as its record now stands, and the call's answer once it is done."""

    return _task(_own(task_id, principal))


def acknowledge(task_id: str, principal: Principal) -> dict[str, Any]:
    """``tasks/update``'s answer. No task asks for input, so there is never an
    outstanding request to fill, and each response is ignored, as the extension says."""

    _own(task_id, principal)
    return {}


def cancel(task_id: str, principal: Principal) -> dict[str, Any]:
    """``tasks/cancel``: the asker takes back what it still may.

    A hold no person has decided is withdrawn. A run nothing has started, the
    task's own or the one its approved hold queued, is cancelled. Cancellation
    is cooperative in the extension, so anything else goes on as it was and
    the request is acknowledged all the same, with the empty result the
    specification requires.
    """

    record = _own(task_id, principal)
    if isinstance(record, ApprovalRequest):
        held = record
        if approvals.outcome(held) is None:
            _as_asker(approvals.WITHDRAW, "approval_id", held, principal, lambda: approvals.outcome(held) is None)
            return {}
        queued = applying(held)
        if queued is None:
            return {}
        record = queued[1]
    operation = record
    if operation.action == OperationRequest.Action.RUN and operation.state == OperationRequest.State.QUEUED:
        _as_asker(
            CANCEL, "operation_id", operation, principal, lambda: operation.state == OperationRequest.State.QUEUED
        )
    return {}


def _as_asker(name: str, field: str, record: Record, principal: Principal, unsettled: Callable[[], bool]) -> None:
    """Run the command ``name`` on ``record`` as the asker would, under that
    command's rules and audit, keyed by the record so a resent cancel acts once.

    Something else that settled it first is what a cooperative cancel accepts;
    any other refusal is the caller's to read.
    """

    answered = run_capability(
        name, {field: str(record.pk)}, principal=principal, idempotency_key=f"tasks-cancel:{record.pk}"
    ).answer
    record.refresh_from_db()
    if answered.get("ok") is False and unsettled():
        raise MCPError(types.INTERNAL_ERROR, refusal_of(answered).message)


def _queued(answer: Mapping[str, Any]) -> str | None:
    work = answer.get(_QUEUED)
    return str(work["id"]) if answer.get("ok") is True and isinstance(work, dict) and work.get("id") else None


def _own(task_id: str, principal: Principal) -> Record:
    found = approvals.own_held_request(task_id, principal=principal) or _own_operation(task_id, principal)
    if found is None:
        raise MCPError(types.INVALID_PARAMS, _UNKNOWN)
    return found


def _own_operation(operation_id: str, principal: Principal) -> OperationRequest | None:
    """The operation ``principal`` itself asked for, by its id, or None, as
    much for another caller's as for an id nothing holds."""

    try:
        return (
            OperationRequest.objects.select_related("resource")
            .filter(
                pk=operation_id,
                requested_actor=principal.actor,
                requested_interface=principal.interface,
                requested_person_key=principal.person_key,
            )
            .first()
        )
    except ValueError, DjangoValidationError:
        return None


def _task(record: Record) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The task, and the call's answer once it is done. A cancelled task
    carries no result (the extension's ``CancelledTask``)."""

    task, answer = _held(record) if isinstance(record, ApprovalRequest) else _run(record)
    return task, None if task["status"] == "cancelled" else answer


def _held(held: ApprovalRequest) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """A hold's task: decided, and then the work its approved call queued, until that ends."""

    approval = approvals.serialize_approval(held)
    settled = approvals.outcome(held)
    now = standing(held)
    cancelled = held.state == ApprovalRequest.State.WITHDRAWN or (
        now.operation is not None and now.operation.state == OperationRequest.State.CANCELLED
    )
    return {
        "taskId": approval["id"],
        "status": _status(cancelled, now.answer),
        "createdAt": approval["requested_at"],
        "lastUpdatedAt": iso(held.updated_at),
        # Until a person decides, the request lapses at its expiry. HQ keeps a
        # settled hold, so its task stays readable however long applying takes.
        "ttlMs": None if settled else int((held.expires_at - held.created_at).total_seconds() * 1000),
        "pollIntervalMs": RUNNING_POLL_INTERVAL_MS if settled else POLL_INTERVAL_MS,
        # On every answer, since a client shows the last one it was sent.
        "statusMessage": f"{approval_review.asked(held)}. {now.said}",
    }, now.answer


def _run(operation: OperationRequest) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The task of work a call queued, said as Jobs says it, until it ends."""

    now = standing_of(run_row(operation))
    asked = operation_asked(operation, queued=True, standing=now)
    answer = ran(asked, _QUEUED, operation, failed=f"{ended(now.said)} `details` holds the work as it ended.")
    return {
        "taskId": str(operation.pk),
        "status": _status(operation.state == OperationRequest.State.CANCELLED, answer),
        "createdAt": iso(operation.created_at),
        "lastUpdatedAt": iso(operation.updated_at),
        # HQ keeps every operation as long as the record it acts on.
        "ttlMs": None,
        "pollIntervalMs": RUNNING_POLL_INTERVAL_MS,
        "statusMessage": now.said,
    }, answer


def _status(cancelled: bool, answer: dict[str, Any] | None) -> str:
    if cancelled:
        return "cancelled"
    return "working" if answer is None else "completed"
