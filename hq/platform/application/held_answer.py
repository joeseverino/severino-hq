"""What a call held for a person answers, typed once for every door that publishes it."""

from typing import Annotated, Any, Literal, TypedDict

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.routes import reverse

from .command_fields import Describe
from .describe import listed
from .hq_self import site_url


class HeldRequest(TypedDict):
    """A call held for approval: what it asks, who asked, and where it is decided."""

    id: Annotated[str, Describe("The request's id, which the asking agent reports back to its user.")]
    capability: Annotated[str, Describe("The command it would run, by name.")]
    target: Annotated[str, Describe("The record the command would act on, as the call named it; empty for none.")]
    resource_kind: Annotated[
        str, Describe("The type of what it would change, which is why it needs approval; empty for none.")
    ]
    resource_key: Annotated[str, Describe("The key of what it would change; empty for none.")]
    state: Annotated[
        str,
        Describe(
            f"Where the request stands: {listed(ApprovalRequest.State.values)}; "
            f"only a `{ApprovalRequest.State.PENDING}` one can still be decided."
        ),
    ]
    requested_actor: Annotated[str, Describe("Who asked, by the name HQ knows the caller by.")]
    requested_interface: Annotated[str, Describe("How it was asked, such as `api` or `mcp`.")]
    reason: Annotated[str, Describe("Why the caller asked, in its own words; empty when it gave none.")]
    requested_at: Annotated[str, Describe("When it was asked, an ISO 8601 date-time.")]
    expires_at: Annotated[str, Describe("When the request lapses undecided, and nothing is written.")]
    content_fingerprint: Annotated[
        str,
        Describe(
            "A digest of the call and of what it was measured against; an approval holds only while both are unchanged."
        ),
    ]
    review_url: Annotated[str, Describe("The absolute address in HQ's web interface where an operator decides it.")]


class AwaitingApproval(TypedDict):
    """A call held for approval, which is no error: nothing is written or queued until an operator approves."""

    status: Annotated[Literal["awaiting_approval"], Describe("That the call waits for approval.")]
    queued: Annotated[Literal[False], Describe("Always false: no work is queued while the call waits.")]
    applied: Annotated[Literal[False], Describe("Always false: nothing changes while the call waits.")]
    message: Annotated[str, Describe("HQ's sentence for the caller: that the change waits, and what to do next.")]
    repeated: Annotated[bool, Describe("Whether this call asked for a request already held.")]
    approval: HeldRequest


class HeldCall(AwaitingApproval):
    """A held call as the capability runner answers it: ``ok``, since nothing failed."""

    ok: Literal[True]


class ApprovalApplied(TypedDict):
    """A held change an operator approved, and what it answered when it ran."""

    ok: Literal[True]
    approved: Annotated[str, Describe("The id of the request approved.")]
    result: Annotated[dict[str, Any], Describe("What the held call answered when it ran, in its own command's shape.")]


class ApprovalRejected(TypedDict):
    """A held change an operator declined, so nothing it asked was done."""

    ok: Literal[True]
    rejected: Annotated[str, Describe("The id of the request rejected.")]


def serialize_approval(held: ApprovalRequest) -> HeldRequest:
    return {
        "id": str(held.id),
        "capability": held.capability,
        "target": held.target,
        "resource_kind": held.resource_kind,
        "resource_key": held.resource_key,
        "state": held.state,
        "requested_actor": held.requested_actor,
        "requested_interface": held.requested_interface,
        "reason": held.reason,
        "requested_at": held.created_at.isoformat(),
        "expires_at": held.expires_at.isoformat(),
        "content_fingerprint": held.content_fingerprint,
        "review_url": site_url(reverse("core:approval_entry", kwargs={"approval_id": held.id})),
    }


# The fields of an answer that carry the operation it queued: ``OperationQueued``'s
# ``operation``, and ``ResourceSaved``'s ``applying``, null where nothing was queued.
_OPERATION_FIELDS = ("operation", "applying")


def applying(held: ApprovalRequest) -> tuple[str, OperationRequest] | None:
    """The operation an approved request's call queued, and the field of its answer that carries it.

    None for a request not approved, or one whose call queued nothing, which
    was done when it ran.
    """

    if held.state != ApprovalRequest.State.APPROVED or not isinstance(held.result, dict):
        return None
    for field in _OPERATION_FIELDS:
        carried = held.result.get(field)
        if isinstance(carried, dict) and carried.get("id"):
            operation = OperationRequest.objects.select_related("resource").filter(pk=carried["id"]).first()
            if operation is not None:
                return field, operation
    return None
