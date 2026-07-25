"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from __future__ import annotations

from typing import Any

from django.db.models import Count
from django.utils import timezone

from application import assets as asset_service
from application.capabilities import (
    describe_capabilities as describe_application_capabilities,
)
from application.capabilities import execute_capability as execute_application_capability
from application import projects as project_service
from application.security import mcp_principal
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


class NotFoundError(ValueError):
    """A requested HQ object does not exist."""


def _write(service, command, **kwargs):
    """Invoke one application mutation as the authenticated MCP principal."""

    return service(command, principal=mcp_principal(), **kwargs)


def describe_capabilities() -> dict[str, Any]:
    """Describe every JSON-executable HQ capability and its canonical schema."""

    return describe_application_capabilities()


def execute_capability(
    name: str,
    payload: dict[str, Any],
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Execute one allowlisted capability from a schema-validated JSON payload."""

    return execute_application_capability(
        name,
        payload,
        principal=mcp_principal(),
        target=target,
        expected_updated_at=expected_updated_at,
    )


def _page_size(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_SIZE)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def list_projects(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List HQ projects, optionally filtered by exact status or text search."""
    return project_service.list_projects(status=status, query=query, limit=limit)


def get_project(slug: str) -> dict[str, Any]:
    """Get one project and its documentation, content, asset, and expense links."""
    try:
        return project_service.get_project(slug)
    except project_service.NotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def list_assets(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List HQ assets, optionally filtered by exact status or text search."""
    return asset_service.list_assets(status=status, query=query, limit=limit)


def get_asset(slug: str) -> dict[str, Any]:
    """Get one asset and its project, documentation, content, and expense links."""
    try:
        return asset_service.get_asset(slug)
    except asset_service.NotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def list_expenses(
    *, year: int | None = None, category: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List expense records with stable relationship identifiers."""
    qs = Expense.objects.select_related(
        "related_project", "related_asset", "related_content", "related_documentation"
    )
    if year is not None:
        qs = qs.filter(date__year=year)
    if category:
        qs = qs.filter(category=category)
    items = [
        {
            "id": expense.id,
            "date": expense.date.isoformat(),
            "vendor": expense.vendor,
            "item": expense.item,
            "category": expense.category,
            "total_cost": str(expense.total_cost),
            "business_use_percentage": expense.business_use_percentage,
            "estimated_deductible_amount": str(
                expense.estimated_deductible_amount
            ),
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
        for expense in qs.order_by("-date", "-id")[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def list_receipts(*, unmatched_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """List receipt metadata only; never returns receipt file contents or URLs."""
    qs = Receipt.objects.all()
    if unmatched_only:
        qs = qs.filter(related_expense__isnull=True, related_asset__isnull=True)
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
        for receipt in qs.select_related("related_asset").order_by("-uploaded_at")[
            : _page_size(limit)
        ]
    ]
    return {"items": items, "count": len(items)}


def documentation_status() -> dict[str, Any]:
    """Summarize AI-safe documentation pointers; sensitive records are excluded."""
    safe = DocumentationRecord.objects.filter(sensitivity__in=SAFE_SENSITIVITIES)
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
    """Return recent HQ audit events without their free-form metadata payloads."""
    items = [
        {
            "id": event.id,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            "object_repr": event.object_repr,
            "message": event.message,
            "created_at": event.created_at.isoformat(),
        }
        for event in AuditLog.objects.order_by("-created_at")[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def system_health() -> dict[str, Any]:
    """Check database access and return non-sensitive record counts."""
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
