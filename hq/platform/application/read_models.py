"""Canonical, non-mutating HQ projections shared by delivery adapters."""

from typing import Literal, TypedDict, cast

from django.db.models import FETCH_RAISE, Count
from django.utils import timezone

from hq.platform.core.models import AuditLog

from . import history, records
from .documentation import DocumentationRow
from .projection import iso, page_size
from .routes import reverse
from .sensitivity import readable_records
lazy from .resources import list_resource
lazy from .security import Principal


class DocumentationStatus(TypedDict):
    total: int
    by_status: dict[str, int]
    by_type: dict[str, int]
    # The first page of the documentation list, as it reads it.
    records: list[DocumentationRow]
    # The documentation list's ``page`` after it.
    next: int | None


def documentation_status(*, principal: Principal) -> DocumentationStatus:
    """Counts of the documentation pointers ``principal`` may read, and the
    first page of them as the documentation list reads it, whose ``next`` it
    is read on by."""

    safe = readable_records(principal).fetch_mode(FETCH_RAISE)
    page = list_resource("documentation", {}, principal=principal)
    return {
        "total": safe.count(),
        "by_status": {
            row["status"]: row["count"] for row in safe.values("status").annotate(count=Count("id")).order_by("status")
        },
        "by_type": {
            row["doc_type"]: row["count"]
            for row in safe.values("doc_type").annotate(count=Count("id")).order_by("doc_type")
        },
        # The list cuts each item to its row, as its kind declares it.
        "records": cast("list[DocumentationRow]", page["items"]),
        "next": page["next"],
    }


class ActivityRow(TypedDict):
    """One audit-log row an event changed."""

    id: int
    action: str
    action_label: str
    object_type: str
    object_id: str
    object_repr: str
    message: str
    actor: str
    created_at: str


class ActivityEvent(TypedDict):
    """One audit-log event: the newest row it changed, a count of the rest, and the page that lists them."""

    id: int | None
    title: str
    action: str
    action_label: str
    actor: str
    # Who started it, through which door, and who approved it, as the audit log says it.
    by: str
    message: str
    created_at: str
    operation_id: str
    items: list[ActivityRow]
    more: int
    url: str


class RecentActivity(TypedDict):
    items: list[ActivityEvent]
    count: int


def _activity_row(event: AuditLog) -> ActivityRow:
    return {
        "id": event.id,
        "action": event.action,
        "action_label": event.get_action_display(),
        "object_type": event.object_type,
        "object_id": event.object_id,
        "object_repr": event.object_repr,
        "message": event.message,
        "actor": event.who.actor,
        # The list's order sets apart the events of one second.
        "created_at": iso(event.created_at, timespec="seconds"),
    }


def recent_activity(*, limit: int = 25) -> RecentActivity:
    """The newest events, as the audit log page reads them: one per operation,
    named, attributed to whoever started it, its outcome as its action, and
    the newest rows it changed beneath it, the rest counted in ``more`` and
    listed at ``url``. No free-form metadata payloads.

    The audit trail is gated apart from baseline reads (``READ_AUDIT_LOG``).
    """

    items: list[ActivityEvent] = [
        {
            "id": line.pk,
            "title": line.title,
            "action": line.action,
            "action_label": line.action_label,
            "actor": line.who.actor,
            "by": line.who.words,
            "message": line.message,
            "created_at": iso(line.at, timespec="seconds"),
            "operation_id": line.events[0].operation_id,
            "items": [_activity_row(item) for item in line.items],
            # The items past those carried, and the page that lists every one.
            "more": line.more,
            "url": reverse("core:audit_detail", args=[line.page.pk]),
        }
        for line in history.recent(limit=page_size(limit))
    ]
    return {"items": items, "count": len(items)}


class SystemHealth(TypedDict):
    status: Literal["ok"]
    checked_at: str
    database: Literal["ok"]
    # Each record domain's own count.
    counts: dict[str, int]


def system_health() -> SystemHealth:
    """Prove database access and return only non-sensitive record counts."""

    return {
        "status": "ok",
        "checked_at": timezone.now().isoformat(),
        "database": "ok",
        # Each record domain's own count, read off its declaration.
        "counts": records.counts(),
    }
