"""What a call held for a person answers, typed once for every door that publishes it."""

from typing import Annotated, Literal, TypedDict

from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.routes import reverse

from .command_fields import Describe
from .describe import listed


class HeldRequest(TypedDict):
    """A call held for a person: what it asks, who asked, and where it is decided."""

    id: Annotated[str, Describe("The request's id, which its asker reports to whoever asked.")]
    capability: Annotated[str, Describe("The command it would run, by name.")]
    target: Annotated[str, Describe("The record the command would act on, as the call named it; empty for none.")]
    resource_kind: Annotated[
        str, Describe("The type of what it would change, which is why it waits for a person; empty for none.")
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
    review_url: Annotated[str, Describe("Where a person decides it, in HQ's web interface.")]


class AwaitingApproval(TypedDict):
    """A call held for a person, which is no error: nothing is written or queued until one agrees."""

    status: Annotated[Literal["awaiting_approval"], Describe("That the call waits for a person.")]
    queued: Annotated[Literal[False], Describe("Whether any work was queued, which it never is while the call waits.")]
    applied: Annotated[Literal[False], Describe("Whether anything changed, which nothing has while the call waits.")]
    message: Annotated[str, Describe("HQ's sentence for the caller: that the change waits, and what to do next.")]
    repeated: Annotated[bool, Describe("Whether this call asked for a request already held.")]
    approval: HeldRequest


class HeldCall(AwaitingApproval):
    """A held call as the capability runner answers it: ``ok``, since nothing failed."""

    ok: Literal[True]


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
        "review_url": reverse("core:approval_entry", kwargs={"approval_id": held.id}),
    }
