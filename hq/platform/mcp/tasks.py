"""A held change as a task of the MCP tasks extension, which a client waits on while a person decides.

The task is the hold. Its id is the hold's, its state is read from the hold at
each ``tasks/get``, and nothing here writes one: a person decides it in HQ, and
no method of the extension reaches a decision. docs/API.md states the
properties.
"""

from collections.abc import Mapping
from typing import Any

from mcp import types
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from hq.platform.application import approvals
from hq.platform.application.security import Principal

IDENTIFIER = "io.modelcontextprotocol/tasks"
# A person answers in minutes or hours, so a poll sooner than this only reads
# the same hold again. It is the longest interval Claude Code honors.
POLL_INTERVAL_MS = 30_000

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


def refuse_cancel(task_id: str, principal: Principal) -> dict[str, Any]:
    """``tasks/cancel``'s answer: a held change stays a person's to decide."""

    _own(task_id, principal)
    raise MCPError(
        types.INVALID_REQUEST,
        "A held change stays waiting for a person, who approves or declines it in HQ; it cannot be cancelled here.",
    )


def _own(task_id: str, principal: Principal) -> Any:
    held = approvals.asked_by(task_id, principal=principal)
    if held is None:
        raise MCPError(types.INVALID_PARAMS, _UNKNOWN)
    return held


def _task(held: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    answer = approvals.outcome(held)
    approval = approvals.serialize_approval(held)
    task: dict[str, Any] = {
        "taskId": approval["id"],
        "status": "working" if answer is None else "completed",
        "createdAt": approval["requested_at"],
        "lastUpdatedAt": held.updated_at.isoformat(),
        "ttlMs": int((held.expires_at - held.created_at).total_seconds() * 1000),
        "pollIntervalMs": POLL_INTERVAL_MS,
    }
    if answer is None:
        task["statusMessage"] = f"Waiting for a person to decide it in HQ, at {approval['review_url']}."
    return task, answer
