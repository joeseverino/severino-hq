"""A held request as its asker reads it, by the id its held answer carried.

One read for every door: the API's record route, MCP's ``get_resource`` and
``hq://approvals/{id}``, and the MCP task a held call becomes, which says the
same standing as a task's state. Only the caller that asked reads a request
(``approvals.own_held_request``); another caller's reads as an id nothing holds.
The commands that decide or withdraw one are declared here too, on the same domain.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest

from . import approval_review, approvals
from .contracts import Effect
from .describe import Describe
from .held_answer import HELD, HeldRequest, applying
from .integration_specs import CapabilitySpec, DetailRead, ListRead, ResourceSpec
from .projection import page_of
from .record_errors import RecordNotFound
from .refusal_codes import Code
from .refusal_status import refusal_of, refuse
from .resource_operations import serialize_operation
from .resources import EmptyQuery
from .security import Capability, Principal
from .ui import ended
lazy from .work_runs import UNENDED


class HeldRequestItem(HeldRequest):
    """A held request as its list names it: what it asks."""

    asks: Annotated[str, Describe("What it asks, in the sentence its card in HQ says.")]


class HeldRequestRecord(HeldRequestItem):
    """A held request you asked for: what it asks, where it stands, and once done what the call answered."""

    said: Annotated[str, Describe("Where it stands, in a sentence.")]
    done: Annotated[
        bool,
        Describe("Whether it is settled: rejected, lapsed, overtaken or withdrawn, or approved and its work ended."),
    ]
    operation: Annotated[
        str,
        Describe(
            "The id of the work its call queued once approved, which `runs` reads a playbook's run by; "
            "empty while it waits, or for a call that queued nothing."
        ),
    ]
    answer: Annotated[
        dict[str, Any],
        Describe(
            "What the call answered once done: its command's own answer, carrying its work as it ended, or a "
            "refusal coded `operator_rejected`, `expired`, `stale`, `withdrawn` or `operation_failed`; "
            "empty until then."
        ),
    ]


@dataclass(frozen=True, slots=True)
class HoldStanding:
    """Where a held request stands: the call's answer once it is done, the
    sentence saying so, and the operation its approved call queued, if any."""

    answer: dict[str, Any] | None
    said: str
    operation: OperationRequest | None = None


def standing(held: ApprovalRequest) -> HoldStanding:
    """``held`` as it stands now: decided, and applied where its call queued work to apply it."""

    settled = approvals.outcome(held)
    review_url = approvals.serialize_approval(held)["review_url"]
    if settled is None:
        return HoldStanding(None, ended(f"It waits for approval in HQ, at {review_url}"))
    if held.state != ApprovalRequest.State.APPROVED:
        return HoldStanding(settled, refusal_of(settled).message)
    queued = applying(held)
    if queued is None:
        return HoldStanding(settled, "It was approved in HQ, and it is done.")
    field, operation = queued
    answered = ran(settled, field, operation, failed=_APPLY_FAILED)
    if answered is None:
        return HoldStanding(None, f"It was approved in HQ, at {review_url}, and HQ is applying it now.", operation)
    if operation.state == OperationRequest.State.SUCCEEDED:
        return HoldStanding(answered, "It was approved in HQ, and it is applied.", operation)
    return HoldStanding(answered, "It was approved in HQ, but applying it failed.", operation)


_APPLY_FAILED = "This change was approved in HQ, but applying it failed. `details` holds the operation."


def ran(answer: Mapping[str, Any], field: str, operation: OperationRequest, *, failed: str) -> dict[str, Any] | None:
    """What a call that queued ``operation`` answers once that has ended: its
    own answer carrying the operation as it ended, or else a refusal coded
    ``operation_failed`` saying ``failed``, with that answer as its details.
    None while the operation is still to end, so the call is not yet done."""

    if operation.state in UNENDED:
        return None
    answered = {**answer, field: serialize_operation(operation)}
    if operation.state == OperationRequest.State.SUCCEEDED:
        return answered
    return refuse(Code.OPERATION_FAILED, failed, answered)


def _item(held: ApprovalRequest) -> dict[str, Any]:
    return {**approvals.serialize_approval(held), "asks": str(approval_review.asked(held))}


def list_held(*, principal: Principal) -> dict[str, Any]:
    """The changes waiting for a person that ``principal`` may read: an agent's own, or every one for a person."""

    return page_of(approvals.pending(principal=principal), each=_item)


def get_held(approval_id: str, *, principal: Principal) -> dict[str, Any]:
    """The held request ``principal`` asked for, by its id."""

    held = approvals.own_held_request(approval_id, principal=principal)
    if held is None:
        # The same answer for another caller's request as for none at all.
        raise RecordNotFound("No held request of yours has that id.")
    now = standing(held)
    return {
        **_item(held),
        "said": now.said,
        "done": now.answer is not None,
        "operation": str(now.operation.pk) if now.operation is not None else "",
        "answer": now.answer or {},
    }


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            HELD,
            "Held requests",
            "A change you asked for that waits for approval, by the `approval.id` its answer carried.",
            Capability.READ,
            listing=ListRead(list_held, EmptyQuery, HeldRequestItem),
            detail=DetailRead(get_held, HeldRequestRecord),
            identifier="id",
            pass_principal=True,
            # Every held request is decided in the audit log's approval queue.
            web_route="core:audit_list",
        ),
    )


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        # A person's decision on a change a credential asked for. Held only in the
        # browser (security.HELD_ONLY_AT), so no credential decides one.
        CapabilitySpec(
            approvals.APPROVE,
            "Agree to a change an agent asked for, and apply exactly what it asked.",
            Effect.REMOTE_WRITE,
            Capability.DECIDE_APPROVALS,
            approvals.ApproveCommand,
            approvals.answered(approvals.approve),
            execution_notes=(
                "Refuse a request already decided or expired, and one you asked for yourself.",
                "Refuse when what it changes moved on after it was asked for.",
                "Run the held call once, recorded under both the asker and the approver.",
            ),
            label="Approve a held change",
            verb="Approve",
            reached_from="action_items",
        ),
        CapabilitySpec(
            approvals.REJECT,
            "Refuse a change an agent asked for, so its request has an answer.",
            Effect.REMOTE_WRITE,
            Capability.DECIDE_APPROVALS,
            approvals.RejectCommand,
            approvals.answered(approvals.reject),
            execution_notes=("Record the refusal, who made it and the note, and apply nothing.",),
            label="Reject a held change",
            verb="Reject",
            reached_from="action_items",
        ),
        # The asker's own way out of a request it no longer wants. Its own
        # request only, so any caller that asked may: a person decides nothing here.
        CapabilitySpec(
            approvals.WITHDRAW,
            "Take back a change you asked for that still waits for approval, so no operator is asked to decide it. "
            "Send the `approval.id` its held answer carried.",
            Effect.REMOTE_WRITE,
            Capability.READ,
            approvals.WithdrawCommand,
            approvals.answered(approvals.withdraw),
            execution_notes=(
                "Find the request among the ones you asked for, and refuse any other.",
                "Refuse a request already decided or expired.",
                "Mark it withdrawn, apply nothing, and stop counting it against how many you may hold.",
            ),
            label="Withdraw a held change",
            verb="Withdraw",
            subject_resource="approvals",
        ),
    )
