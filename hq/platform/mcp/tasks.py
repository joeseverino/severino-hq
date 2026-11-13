"""A held change as a task of the MCP tasks extension, which a client waits on while a person decides and HQ applies it.

The task is the hold. Its id is the hold's, its state is read from the hold,
and from the operation its call queued, at each ``tasks/get``. A person
decides it in HQ; ``tasks/cancel`` is the asker withdrawing it, through the
same command a call of ``approval.withdraw`` runs. docs/API.md states the
properties.
"""

from collections.abc import Mapping
from typing import Any

from mcp import types
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application import approval_review, approvals
from hq.platform.application.capabilities import run_capability
from hq.platform.application.held_reads import standing
from hq.platform.application.refusal_status import refusal_of
from hq.platform.application.security import Principal

IDENTIFIER = "io.modelcontextprotocol/tasks"
# A person answers in minutes or hours, so a poll sooner than this only reads
# the same hold again. It is the longest interval Claude Code honors.
POLL_INTERVAL_MS = 30_000
# The controller applies a change in seconds, so an approved one is read again soon.
APPLYING_POLL_INTERVAL_MS = 2_000

# The one answer for an id this caller did not ask for, whether or not it exists.
_UNKNOWN = "No task of this caller has that id."


class UpdateTaskRequestParams(types.GetTaskRequestParams):
    """``tasks/update``: answers to a task's input requests, which a hold never makes."""

    input_responses: dict[str, Any] = Field(default_factory=dict)


def requested(context: Any) -> bool:
    """Whether this request's client takes a task in place of the held answer.

    The extension's result is 2026-07-28 vocabulary, so only a client on that
    wire that declared it on this request is given one.
    """

    if context is None or context.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return False
    declared = context.session.client_capabilities
    return bool(declared and declared.extensions and IDENTIFIER in declared.extensions)


def created(answer: Mapping[str, Any], principal: Principal) -> dict[str, Any] | None:
    """The task for a held answer, or None when the hold is not this caller's.

    A repeat of another caller's request is answered with that request, which
    this caller could not read as a task, so it keeps the held answer.
    """

    held = approvals.asked_by(str(answer["approval"]["id"]), principal=principal)
    if held is None:
        return None
    task, _ = _task(held)
    return {"resultType": "task", **task}


def state(task_id: str, principal: Principal) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The task's fields as the hold now stands, and the call's answer once it is settled."""

    return _task(_own(task_id, principal))


def acknowledge(task_id: str, principal: Principal) -> dict[str, Any]:
    """``tasks/update``'s answer. A hold asks for no input, so there is never an
    outstanding request to fill, and each response is ignored, as the extension says."""

    _own(task_id, principal)
    return {}


def cancel(task_id: str, principal: Principal) -> dict[str, Any]:
    """``tasks/cancel``: the asker withdraws its hold while it still waits.

    Cancellation is cooperative in the extension, so a hold already decided
    goes on as it was and the request is acknowledged all the same, with the
    empty result the specification requires.
    """

    held = _own(task_id, principal)
    if approvals.outcome(held) is None:
        # Keyed by the hold, so a resent cancel withdraws it once.
        ran = run_capability(
            approvals.WITHDRAW,
            {"approval_id": str(held.pk)},
            principal=principal,
            idempotency_key=f"tasks-cancel:{held.pk}",
        )
        # A person who decided it first settled it, which a cooperative cancel
        # accepts; any other refusal is the caller's to read.
        held.refresh_from_db()
        if ran.answer.get("ok") is False and approvals.outcome(held) is None:
            raise MCPError(types.INTERNAL_ERROR, refusal_of(ran.answer).message)
    return {}


def _own(task_id: str, principal: Principal) -> Any:
    held = approvals.asked_by(task_id, principal=principal)
    if held is None:
        raise MCPError(types.INVALID_PARAMS, _UNKNOWN)
    return held


def _task(held: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The task, and the call's answer once it is done: decided, and applied
    where the call queued an operation to apply it."""

    approval = approvals.serialize_approval(held)
    settled = approvals.outcome(held)
    now = standing(held)
    answer, said = now.answer, now.said
    task: dict[str, Any] = {
        "taskId": approval["id"],
        "status": _status(held, answer),
        "createdAt": approval["requested_at"],
        "lastUpdatedAt": held.updated_at.isoformat(),
        # Until a person decides, the request lapses at its expiry. HQ keeps a
        # settled hold, so its task stays readable however long applying takes.
        "ttlMs": None if settled else int((held.expires_at - held.created_at).total_seconds() * 1000),
        "pollIntervalMs": APPLYING_POLL_INTERVAL_MS if settled else POLL_INTERVAL_MS,
        # On every answer, since a client shows the last one it was sent.
        "statusMessage": f"{approval_review.asked(held)}. {said}",
    }
    # A cancelled task carries no result (the extension's ``CancelledTask``).
    return task, None if task["status"] == "cancelled" else answer


def _status(held: Any, answer: dict[str, Any] | None) -> str:
    if held.state == ApprovalRequest.State.WITHDRAWN:
        return "cancelled"
    return "working" if answer is None else "completed"
