"""Canonical, non-mutating HQ projections shared by delivery adapters."""

from datetime import datetime
from typing import Any, Literal, TypedDict, cast

from django.db.models import FETCH_RAISE, Count
from django.utils import timezone

from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.platform.core.models import AuditLog

from . import history, records
from .expenses import serialize_expense
from .projection import NextPage, listing, page_size
from .receipts import serialize_receipt
from .resource_records import DocumentationRow
from .routes import reverse
from .sensitivity import safe_records
lazy from .resources import list_resource
lazy from .security import Capability, Principal


def list_expenses(*, year: int | None = None, category: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List expense records with stable, sensitivity-safe relationships."""
    rows = Expense.objects.select_related(
        "related_project", "related_asset", "related_content", "related_documentation"
    )
    if year is not None:
        rows = rows.filter(date__year=year)
    if category:
        rows = rows.filter(category=category)
    return listing(rows, serialize_expense, order=("-date", "-id"), limit=limit)


def list_receipts(*, unmatched_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """List receipt metadata without file contents, storage paths, or URLs."""
    rows = Receipt.objects.select_related("related_asset")
    if unmatched_only:
        rows = rows.filter(related_expense__isnull=True, related_asset__isnull=True)
    return listing(rows, serialize_receipt, order=("-uploaded_at",), limit=limit)


class DocumentationStatus(TypedDict):
    total: int
    by_status: dict[str, int]
    by_type: dict[str, int]
    # The first page of the documentation list, as it reads it.
    records: list[DocumentationRow]
    next: NextPage | None


def documentation_status(*, principal: Principal) -> DocumentationStatus:
    """Counts of the AI-safe documentation pointers, and the first page of them
    as the documentation list reads it, whose ``next`` it is read on by."""

    principal.require(Capability.READ)
    safe = safe_records().fetch_mode(FETCH_RAISE)
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
    message: str
    created_at: str
    operation_id: str
    items: list[ActivityRow]
    more: int
    url: str


class RecentActivity(TypedDict):
    items: list[ActivityEvent]
    count: int


def _second(at: datetime) -> str:
    """When an event happened, to the second: the list's order already sets
    apart the events of one second, so a fraction is bytes no reader needs."""

    return at.isoformat(timespec="seconds")


def _activity_row(event: AuditLog) -> ActivityRow:
    return {
        "id": event.id,
        "action": event.action,
        "action_label": event.get_action_display(),
        "object_type": event.object_type,
        "object_id": event.object_id,
        "object_repr": event.object_repr,
        "message": event.message,
        "actor": event.actor_label,
        "created_at": _second(event.created_at),
    }


def recent_activity(*, principal: Principal, limit: int = 25) -> RecentActivity:
    """The newest events, as the audit log page reads them: one per operation,
    named, attributed to whoever started it, its outcome as its action, and
    the newest rows it changed beneath it, the rest counted in ``more`` and
    listed at ``url``. No free-form metadata payloads.

    The audit trail is gated apart from baseline reads (``READ_AUDIT_LOG``).
    """

    principal.require(Capability.READ_AUDIT_LOG)
    items: list[ActivityEvent] = [
        {
            "id": line.pk,
            "title": line.title,
            "action": line.action,
            "action_label": line.action_label,
            "actor": line.actor,
            "message": line.message,
            "created_at": _second(line.at),
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


def system_health(*, principal: Principal) -> SystemHealth:
    """Prove database access and return only non-sensitive record counts."""

    principal.require(Capability.READ)
    return {
        "status": "ok",
        "checked_at": timezone.now().isoformat(),
        "database": "ok",
        # Each record domain's own count, read off its declaration.
        "counts": records.counts(),
    }
