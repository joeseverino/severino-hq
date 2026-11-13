"""A held change as a task of the MCP tasks extension, which a client waits on while a person decides and HQ applies it.

The task is the hold. Its id is the hold's, its state is read from the hold,
and from the operation its call queued, at each ``tasks/get``, and nothing
here writes either: a person decides it in HQ, and no method of the extension
reaches a decision. docs/API.md states the properties.
"""

from collections.abc import Mapping
from typing import Any

from mcp import types
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application import approval_review, approvals
from hq.platform.application.held_answer import applying
from hq.platform.application.refusal_status import refusal_of, refuse
from hq.platform.application.resource_operations import serialize_operation
from hq.platform.application.security import Principal

IDENTIFIER = "io.modelcontextprotocol/tasks"
# A person answers in minutes or hours, so a poll sooner than this only reads
# the same hold again. It is the longest interval Claude Code honors.
POLL_INTERVAL_MS = 30_000
# The controller applies a change in seconds, so an approved one is read again soon.
APPLYING_POLL_INTERVAL_MS = 2_000

# An operation still to finish, so the call it answers is not yet done.
_UNFINISHED = frozenset({OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED})

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
    """``tasks/cancel``'s acknowledgement. Cancellation is the server's to honor
    or not, and a held change stays a person's to decide, so the task goes on
    as it was."""

    _own(task_id, principal)
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
    answer, said = _standing(held, settled, approval["review_url"])
    task: dict[str, Any] = {
        "taskId": approval["id"],
        "status": "working" if answer is None else "completed",
        "createdAt": approval["requested_at"],
        "lastUpdatedAt": held.updated_at.isoformat(),
        # Until a person decides, the request lapses at its expiry. HQ keeps a
        # settled hold, so its task stays readable however long applying takes.
        "ttlMs": None if settled else int((held.expires_at - held.created_at).total_seconds() * 1000),
        "pollIntervalMs": APPLYING_POLL_INTERVAL_MS if settled else POLL_INTERVAL_MS,
        # On every answer, since a client shows the last one it was sent.
        "statusMessage": f"{approval_review.asked(held)}. {said}",
    }
    return task, answer


def _standing(held: Any, settled: dict[str, Any] | None, review_url: str) -> tuple[dict[str, Any] | None, str]:
    """The call's answer, None until it is done, and the sentence saying where it stands."""

    if settled is None:
        return None, f"A person decides it in HQ, at {review_url}."
    if held.state != ApprovalRequest.State.APPROVED:
        return settled, refusal_of(settled).message
    queued = applying(held)
    if queued is None:
        return settled, "A person approved it in HQ, and it is done."
    field, operation = queued
    if operation.state in _UNFINISHED:
        return None, f"A person approved it in HQ, at {review_url}, and HQ is applying it now."
    # The call's own answer, carrying its operation as it ended.
    ended = {**settled, field: serialize_operation(operation)}
    if operation.state == OperationRequest.State.SUCCEEDED:
        return ended, "A person approved it in HQ, and it is applied."
    failed = refuse(
        "operation_failed",
        "A person approved this change in HQ, and applying it did not succeed; details carries the operation.",
        ended,
    )
    return failed, "A person approved it in HQ, and applying it did not succeed."
