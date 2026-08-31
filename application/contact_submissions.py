"""Authorized Cloudflare D1 contact-submission use cases.

The transport lives in ``contacts.d1``. This module is the shared application
boundary used by web, Command Center, API, and MCP, so an external write cannot
quietly acquire different validation or audit behavior in each adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.audit import operation_context, record_event
from core.models import AuditLog
from contacts import d1

from .security import Capability, Principal


CONTACT_STATUSES = frozenset({"unread", "read", "replied", "archived", "spam"})


class ContactSubmissionNotFound(ValueError):
    pass


@dataclass(frozen=True)
class ContactListCommand:
    status: str = ""
    query: str = ""
    limit: int = 100


@dataclass(frozen=True)
class ContactReviewCommand:
    status: str
    assigned_to: str = ""
    admin_notes: str = ""


@dataclass(frozen=True)
class ContactDeleteCommand:
    confirm: str


def _status(value: str, *, allow_blank: bool = False) -> str:
    value = value.strip()
    if not value and allow_blank:
        return ""
    if value not in CONTACT_STATUSES:
        raise ValueError(f"Unknown contact status {value!r}.")
    return value


def _submission(identifier: int) -> dict[str, Any]:
    found = d1.get_submission(identifier)
    if found is None:
        raise ContactSubmissionNotFound(
            f"Contact submission #{identifier} was not found."
        )
    return found


def list_contact_submissions(
    *, status: str = "", query: str = "", limit: int = 100
) -> dict[str, Any]:
    status = _status(status, allow_blank=True)
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    items = d1.list_submissions(status=status, q=query.strip(), limit=limit)
    return {"items": items, "count": len(items)}


def get_contact_submission(identifier: int) -> dict[str, Any]:
    return _submission(identifier)


def execute_contact_list(
    command: ContactListCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    principal.require(Capability.MANAGE_CONTACTS)
    return {"ok": True, **list_contact_submissions(
        status=command.status,
        query=command.query,
        limit=command.limit,
    )}


def execute_contact_review(
    command: ContactReviewCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    principal.require(Capability.MANAGE_CONTACTS)
    status = _status(command.status)
    if len(command.assigned_to) > 120:
        raise ValueError("assigned_to must be at most 120 characters")
    before = _submission(current_id)
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="contact.submission.review",
    ):
        d1.update_submission(
            current_id,
            status,
            command.assigned_to.strip(),
            command.admin_notes,
        )
        record_event(
            action=AuditLog.Action.UPDATED,
            type_label="Contact submission",
            message=f"Reviewed contact submission #{current_id}.",
            metadata={
                "id": current_id,
                "status": status,
                "previous_status": before.get("status", ""),
            },
            required=True,
        )
    return {"ok": True, "id": current_id, "status": status}


def execute_contact_delete(
    command: ContactDeleteCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    principal.require(Capability.MANAGE_CONTACTS)
    if command.confirm != str(current_id):
        raise ValueError(f"confirm must exactly match target {current_id!r}")
    try:
        before = _submission(current_id)
    except ContactSubmissionNotFound:
        # A retry after D1 accepted the delete is already at the requested end
        # state. This keeps the external operation domain-idempotent too.
        return {"ok": True, "deleted": {"id": current_id, "already_absent": True}}
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="contact.submission.delete",
    ):
        d1.delete_submission(current_id)
        record_event(
            action=AuditLog.Action.DELETED,
            type_label="Contact submission",
            message=f"Deleted contact submission #{current_id}.",
            metadata={"id": current_id, "status": before.get("status", "")},
            required=True,
        )
    return {"ok": True, "deleted": {"id": current_id, "already_absent": False}}
