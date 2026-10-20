"""Canonical, non-mutating HQ projections shared by delivery adapters."""

from typing import Any

from django.db.models import FETCH_RAISE, Count
from django.utils import timezone

from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.platform.core.models import AuditLog

from . import records
from .expenses import serialize_expense
from .projection import iso, listing, page_size
from .receipts import serialize_receipt
from .sensitivity import SAFE_SENSITIVITIES
lazy from .security import Capability


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


def documentation_status() -> dict[str, Any]:
    """Summarize AI-safe documentation pointers; sensitive records stay excluded."""
    safe = DocumentationRecord.objects.filter(sensitivity__in=SAFE_SENSITIVITIES).fetch_mode(FETCH_RAISE)
    return {
        "total": safe.count(),
        "by_status": {
            row["status"]: row["count"] for row in safe.values("status").annotate(count=Count("id")).order_by("status")
        },
        "by_type": {
            row["doc_type"]: row["count"]
            for row in safe.values("doc_type").annotate(count=Count("id")).order_by("doc_type")
        },
        "records": [
            {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "type": doc.doc_type,
                "system": doc.system_service,
                "environment": doc.environment,
                "status": doc.status,
                "sensitivity": doc.sensitivity,
                "obsidian_path": doc.obsidian_path,
                "github_path": doc.github_path,
                "external_url": doc.external_url,
                "last_reviewed": iso(doc.last_reviewed),
            }
            for doc in safe.order_by("doc_id")
        ],
    }


def recent_activity(*, principal, limit: int = 25) -> dict[str, Any]:
    """Return stable audit summaries without free-form metadata payloads.

    The audit trail is gated apart from baseline reads (``READ_AUDIT_LOG``).
    """

    principal.require(Capability.READ_AUDIT_LOG)
    items = [
        {
            "id": event.id,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            "object_repr": event.object_repr,
            "message": event.message,
            "actor": event.actor_label,
            "action_label": event.get_action_display(),
            "created_at": event.created_at.isoformat(),
        }
        for event in AuditLog.objects.select_related("user")
        .fetch_mode(FETCH_RAISE)
        .order_by("-created_at")[: page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def system_health() -> dict[str, Any]:
    """Prove database access and return only non-sensitive record counts."""
    return {
        "status": "ok",
        "checked_at": timezone.now().isoformat(),
        "database": "ok",
        # Each record domain's own count, read off its declaration.
        "counts": records.counts(),
    }
