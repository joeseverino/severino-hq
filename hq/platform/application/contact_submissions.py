"""Authorized contact-submission use cases, shared by web, Command Center, API
and MCP so validation and audit cannot differ by adapter.

Reads come from the stored reading (``contacts.inbox``). A change is written to
the ``cloudflare.contact_inbox`` declaration and queued for the controller,
which applies it through the cloudflare_api connection: HQ holds no credential
for D1.
"""

from dataclasses import dataclass
from typing import Annotated, Any, get_args

from django.core.exceptions import ValidationError
from django.db import transaction

from hq.domains.contacts import inbox
from hq.domains.control_plane.desired_state import desired_fingerprint
from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.cloudflare import CONTACT_CHANGES_LIMIT, ContactStatus
from hq.domains.control_plane.providers import validate_spec
from hq.platform.core.audit import operation_context, record_event
from hq.platform.core.models import AuditLog

from .adoption import OBSERVES_ONLY, observes_only
from .cadence import request_reading
from .command_fields import Describe
from .record_errors import PolicyError, RecordNotFound
from .resource_operations import queue_reconcile
from .security import Principal

CONTACT_STATUSES = frozenset(get_args(ContactStatus))
ASSIGNEE_LENGTH = 120
REVIEW = "contact.submission.review"
DELETE = "contact.submission.delete"


@dataclass(frozen=True, slots=True)
class ContactListCommand:
    status: Annotated[str, Describe("Only submissions in this state; blank for all.")] = ""
    query: Annotated[str, Describe("Only submissions mentioning these words.")] = ""
    limit: Annotated[int, Describe("At most this many submissions.")] = 100


@dataclass(frozen=True, slots=True)
class ContactReviewCommand:
    status: Annotated[str, Describe("Where the submission stands.")]
    assigned_to: Annotated[str, Describe("Who is handling it.")] = ""
    admin_notes: Annotated[str, Describe("Notes on it, for HQ only.")] = ""


@dataclass(frozen=True, slots=True)
class ContactDeleteCommand:
    confirm: Annotated[str, Describe("The submission's ID again, to confirm the delete.")]


def _status(value: str, *, allow_blank: bool = False) -> str:
    value = value.strip()
    if not value and allow_blank:
        return ""
    if value not in CONTACT_STATUSES:
        raise ValidationError({"status": ValidationError("Unknown contact status.", code="invalid_choice")})
    return value


def _matches(row: dict[str, Any], status: str, wanted: str) -> bool:
    if status and row.get("status") != status:
        return False
    return not wanted or any(wanted in str(row.get(name) or "").casefold() for name in ("name", "email", "message"))


def list_contact_submissions(*, status: str = "", query: str = "", limit: int = 100) -> dict[str, Any]:
    status = _status(status, allow_blank=True)
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    wanted = query.strip().casefold()
    items = [row for row in inbox.current().rows if _matches(row, status, wanted)][:limit]
    return {"items": items, "count": len(items)}


def find_submission(identifier: int, current: inbox.Inbox | None = None) -> dict[str, Any] | None:
    """One stored message with HQ's changes laid over, or None."""

    rows = (current or inbox.current()).rows
    return next((row for row in rows if int(row["id"]) == identifier), None)


def _submission(identifier: int, current: inbox.Inbox) -> dict[str, Any]:
    found = find_submission(identifier, current)
    if found is None:
        raise RecordNotFound("That contact submission was not found.")
    return found


def get_contact_submission(identifier: int) -> dict[str, Any]:
    return _submission(identifier, inbox.current())


def execute_contact_list(
    command: ContactListCommand,
    *,
    principal: Principal,
) -> dict[str, Any]:
    del principal
    return {
        "ok": True,
        **list_contact_submissions(
            status=command.status,
            query=command.query,
            limit=command.limit,
        ),
    }


def _unconfirmed(spec: dict[str, Any], current: inbox.Inbox) -> dict[str, Any]:
    """The declaration without the changes the reading already shows.

    What remains is what the controller has yet to apply, so the declaration
    stays a short backlog rather than a growing history.
    """

    waiting = {int(row["id"]) for row in current.rows if row.get("pending")}
    reviews = [review for review in spec.get("reviews") or () if int(review["id"]) in waiting]
    deletions = [identifier for identifier in spec.get("deletions") or () if int(identifier) in current.held]
    return {**spec, "reviews": reviews, "deletions": deletions}


def _change(current: inbox.Inbox, change: dict[str, Any]) -> dict[str, Any]:
    """The declaration's next spec: the unconfirmed changes plus this one."""

    database = current.database
    if database is None:
        raise PolicyError("No contact messages have been read yet.")
    resource = inbox.declaration()
    previous = resource.spec if resource is not None else {}
    spec = _unconfirmed(previous, current) if inbox.Database(**_ids(previous)) == database else {}
    reviews = [review for review in spec.get("reviews") or () if int(review["id"]) != change.get("id")]
    deletions = [identifier for identifier in spec.get("deletions") or () if int(identifier) != change.get("delete")]
    if "delete" in change:
        deletions.append(change["delete"])
    else:
        reviews.append(change)
    if len(reviews) > CONTACT_CHANGES_LIMIT or len(deletions) > CONTACT_CHANGES_LIMIT:
        raise PolicyError("Too many changes are waiting for the controller. Try again once it has run.")
    return {
        "connection_ref": database.connection_ref,
        "account_id": database.account_id,
        "database_id": database.database_id,
        "reviews": reviews,
        "deletions": deletions,
    }


def _ids(spec: dict[str, Any]) -> dict[str, str]:
    return {name: str(spec.get(name, "")) for name in ("connection_ref", "account_id", "database_id")}


def _declare(spec: dict[str, Any], *, principal: Principal) -> None:
    """Write the declaration and queue the controller to apply it."""

    validated = validate_spec(inbox.DECLARATION, spec)
    if observes_only(inbox.DECLARATION, validated):
        raise PolicyError(f"{OBSERVES_ONLY} The Cloudflare API connection also needs D1 Write (account).")
    resource = ManagedResource.objects.select_for_update().filter(key=inbox.DECLARATION_KEY).first()
    if resource is None:
        resource = ManagedResource(key=inbox.DECLARATION_KEY, kind=inbox.DECLARATION)
    elif resource.spec != validated:
        resource.generation += 1
    resource.spec = validated
    resource.enabled = True
    resource.desired_fingerprint = desired_fingerprint(
        resource.kind, validated, enabled=True, resource_key=resource.key
    )
    resource.full_clean()
    resource.save()
    queue_reconcile(resource, principal=principal, reason="A contact message was changed.")
    transaction.on_commit(lambda: request_reading(inbox.READING))


@transaction.atomic
def execute_contact_review(
    command: ContactReviewCommand,
    *,
    principal: Principal,
    current_id: int,
) -> dict[str, Any]:
    status = _status(command.status)
    if len(command.assigned_to) > ASSIGNEE_LENGTH:
        too_long = ValidationError("Too long.", code="max_length", params={"limit_value": ASSIGNEE_LENGTH})
        raise ValidationError({"assigned_to": too_long})
    current = inbox.current()
    before = _submission(current_id, current)
    review = {
        "id": current_id,
        "status": status,
        "assigned_to": command.assigned_to.strip(),
        "admin_notes": command.admin_notes,
    }
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation=REVIEW,
    ):
        _declare(_change(current, review), principal=principal)
        record_event(
            action=AuditLog.Action.UPDATED,
            type_label="Contact submission",
            message=f"Asked for contact submission #{current_id} to be reviewed.",
            metadata={
                "id": current_id,
                "status": status,
                "previous_status": before.get("status", ""),
            },
            required=True,
        )
    return {"ok": True, "id": current_id, "status": status, "queued": True}


@transaction.atomic
def execute_contact_delete(
    command: ContactDeleteCommand,
    *,
    principal: Principal,
    current_id: int,
) -> dict[str, Any]:
    if command.confirm != str(current_id):
        raise ValidationError({"confirm": ValidationError("Must match the message.", code="invalid")})
    current = inbox.current()
    before = find_submission(current_id, current)
    if before is None:
        # Already gone, or already waiting to be deleted: the requested end
        # state either way, so a retry succeeds.
        return {"ok": True, "deleted": {"id": current_id, "already_absent": True}}
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation=DELETE,
    ):
        _declare(_change(current, {"delete": current_id}), principal=principal)
        record_event(
            action=AuditLog.Action.DELETED,
            type_label="Contact submission",
            message=f"Asked for contact submission #{current_id} to be deleted.",
            metadata={"id": current_id, "status": before.get("status", "")},
            required=True,
        )
    return {"ok": True, "deleted": {"id": current_id, "already_absent": False}, "queued": True}
