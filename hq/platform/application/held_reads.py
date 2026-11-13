"""A held request as its asker reads it, by the id its held answer carried.

One read for every door: the API's record route, MCP's ``get_resource`` and
``hq://approvals/{id}``, and the MCP task a held call becomes, which says the
same standing as a task's state. Only the caller that asked reads a request
(``approvals.asked_by``); another caller's reads as an id nothing holds.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest

from . import approval_review, approvals
from .describe import Describe
from .held_answer import HELD, HeldRequest, applying
from .integration_specs import ResourceSpec
from .record_errors import RecordNotFound
from .refusal_status import refusal_of, refuse
from .resource_operations import serialize_operation
from .security import Capability, Principal
from .ui import ended

# An operation still to finish, so the call it answers is not yet done.
UNFINISHED = frozenset({OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED})


class HeldRequestRecord(HeldRequest):
    """A held request you asked for: what it asks, where it stands, and once done what the call answered."""

    asks: Annotated[str, Describe("What it asks, in the sentence its card in HQ says.")]
    said: Annotated[str, Describe("Where it stands, in a sentence.")]
    done: Annotated[
        bool,
        Describe("Whether it is settled: declined, lapsed, overtaken or withdrawn, or approved and its work ended."),
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
            "refusal coded `declined`, `expired`, `stale`, `withdrawn` or `operation_failed`; empty until then."
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
    if operation.state in UNFINISHED:
        return HoldStanding(None, f"It was approved in HQ, at {review_url}, and HQ is applying it now.", operation)
    # The call's own answer, carrying its operation as it ended.
    answered = {**settled, field: serialize_operation(operation)}
    if operation.state == OperationRequest.State.SUCCEEDED:
        return HoldStanding(answered, "It was approved in HQ, and it is applied.", operation)
    failed = refuse(
        "operation_failed",
        "This change was approved in HQ, but applying it failed. `details` holds the operation.",
        answered,
    )
    return HoldStanding(failed, "It was approved in HQ, but applying it failed.", operation)


def get_held(approval_id: str, *, principal: Principal) -> dict[str, Any]:
    """The held request ``principal`` asked for, by its id."""

    held = approvals.asked_by(approval_id, principal=principal)
    if held is None:
        # The same answer for another caller's request as for none at all.
        raise RecordNotFound("No held request of yours has that id.")
    now = standing(held)
    return {
        **approvals.serialize_approval(held),
        "asks": str(approval_review.asked(held)),
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
            detail_handler=get_held,
            identifier="id",
            pass_principal=True,
            record_type=HeldRequestRecord,
        ),
    )
