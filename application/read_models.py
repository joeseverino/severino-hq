"""Canonical, non-mutating HQ projections shared by delivery adapters."""

from __future__ import annotations

from typing import Any

from django.db.models import FETCH_RAISE, Count
from django.utils import timezone

from assets.models import Asset
from content.models import ContentItem
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt

SAFE_SENSITIVITIES = (
    DocumentationRecord.Sensitivity.PUBLIC,
    DocumentationRecord.Sensitivity.INTERNAL,
)
MAX_PAGE_SIZE = 100


def _page_size(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_SIZE)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def list_expenses(
    *, year: int | None = None, category: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List expense records with stable, sensitivity-safe relationships."""
    queryset = Expense.objects.select_related(
        "related_project", "related_asset", "related_content", "related_documentation"
    ).fetch_mode(FETCH_RAISE)
    if year is not None:
        queryset = queryset.filter(date__year=year)
    if category:
        queryset = queryset.filter(category=category)
    items = [
        {
            "id": expense.id,
            "date": expense.date.isoformat(),
            "vendor": expense.vendor,
            "item": expense.item,
            "category": expense.category,
            "total_cost": str(expense.total_cost),
            "business_use_percentage": expense.business_use_percentage,
            "estimated_deductible_amount": str(expense.estimated_deductible_amount),
            "business_purpose": expense.business_purpose,
            "related_project": (
                expense.related_project.slug if expense.related_project else None
            ),
            "related_asset": (
                expense.related_asset.slug if expense.related_asset else None
            ),
            "related_content": (
                expense.related_content.slug if expense.related_content else None
            ),
            "related_documentation": (
                expense.related_documentation.doc_id
                if expense.related_documentation
                and expense.related_documentation.sensitivity in SAFE_SENSITIVITIES
                else None
            ),
        }
        for expense in queryset.order_by("-date", "-id")[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def list_receipts(*, unmatched_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """List receipt metadata without file contents, storage paths, or URLs."""
    queryset = Receipt.objects.fetch_mode(FETCH_RAISE)
    if unmatched_only:
        queryset = queryset.filter(
            related_expense__isnull=True, related_asset__isnull=True
        )
    items = [
        {
            "id": receipt.id,
            "original_filename": receipt.original_filename,
            "content_type": receipt.content_type,
            "size_bytes": receipt.size_bytes,
            "vendor": receipt.vendor,
            "date": _iso(receipt.date),
            "amount": str(receipt.amount),
            "related_expense_id": receipt.related_expense_id,
            "related_asset": (
                receipt.related_asset.slug if receipt.related_asset else None
            ),
            "uploaded_at": _iso(receipt.uploaded_at),
        }
        for receipt in queryset.select_related("related_asset").order_by(
            "-uploaded_at"
        )[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def documentation_status() -> dict[str, Any]:
    """Summarize AI-safe documentation pointers; sensitive records stay excluded."""
    safe = DocumentationRecord.objects.filter(
        sensitivity__in=SAFE_SENSITIVITIES
    ).fetch_mode(FETCH_RAISE)
    return {
        "total": safe.count(),
        "by_status": {
            row["status"]: row["count"]
            for row in safe.values("status")
            .annotate(count=Count("id"))
            .order_by("status")
        },
        "by_type": {
            row["doc_type"]: row["count"]
            for row in safe.values("doc_type")
            .annotate(count=Count("id"))
            .order_by("doc_type")
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
                "last_reviewed": _iso(doc.last_reviewed),
            }
            for doc in safe.order_by("doc_id")
        ],
    }


def recent_activity(*, limit: int = 25) -> dict[str, Any]:
    """Return stable audit summaries without free-form metadata payloads."""
    items = [
        {
            "id": event.id,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            "object_repr": event.object_repr,
            "message": event.message,
            "actor": event.user.get_username() if event.user_id else "system",
            "action_label": event.get_action_display(),
            "created_at": event.created_at.isoformat(),
        }
        for event in AuditLog.objects.select_related("user")
        .fetch_mode(FETCH_RAISE)
        .order_by("-created_at")[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def system_health() -> dict[str, Any]:
    """Prove database access and return only non-sensitive record counts."""
    return {
        "status": "ok",
        "checked_at": timezone.now().isoformat(),
        "database": "ok",
        "counts": {
            "projects": Project.objects.count(),
            "assets": Asset.objects.count(),
            "expenses": Expense.objects.count(),
            "receipts": Receipt.objects.count(),
            "content": ContentItem.objects.count(),
            "documentation_safe": DocumentationRecord.objects.filter(
                sensitivity__in=SAFE_SENSITIVITIES
            ).count(),
        },
    }
