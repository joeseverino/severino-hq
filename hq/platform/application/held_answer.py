"""What a call held for a person answers, typed once for every door that publishes it."""

from typing import Annotated, Literal, TypedDict

from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.routes import reverse

from .command_fields import Describe


class HeldRequest(TypedDict):
    """A call held for a person: what it asks, who asked, and where it is decided."""

    id: Annotated[str, Describe("The request's id, which its asker reports to whoever asked.")]
    capability: str
    target: str
    resource_kind: str
    resource_key: str
    state: str
    requested_actor: str
    requested_interface: str
    reason: str
    requested_at: str
    expires_at: Annotated[str, Describe("When the request lapses undecided, and nothing is written.")]
    content_fingerprint: str
    review_url: Annotated[str, Describe("Where a person decides it, in HQ's web interface.")]


class AwaitingApproval(TypedDict):
    """A call held for a person, which is no error: nothing is written or queued until one agrees."""

    status: Literal["awaiting_approval"]
    queued: Literal[False]
    applied: Literal[False]
    message: str
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
