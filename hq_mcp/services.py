"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q
from django.utils import timezone

from application import documentation as documentation_service
from application import projects as project_service
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


def create_project(
    name: str,
    slug: str = "",
    category: str = "other",
    status: str = "idea",
    description: str = "",
    technologies: str = "",
    repository_url: str = "",
    public_url: str = "",
    deployment_notes: str = "",
    security_notes: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create an HQ project through the canonical validated service."""
    return project_service.save_project(
        project_service.ProjectCommand(
            name=name,
            slug=slug,
            category=category,
            status=status,
            description=description,
            technologies_used=technologies,
            repository_url=repository_url,
            public_url=public_url,
            deployment_notes=deployment_notes,
            security_notes=security_notes,
            notes=notes,
        ),
        interface="mcp",
        actor="mcp-service-account",
    )


def update_project(
    slug: str,
    name: str,
    new_slug: str = "",
    category: str = "other",
    status: str = "idea",
    description: str = "",
    technologies: str = "",
    repository_url: str = "",
    public_url: str = "",
    deployment_notes: str = "",
    security_notes: str = "",
    notes: str = "",
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Update an HQ project with optional optimistic concurrency protection."""
    return project_service.save_project(
        project_service.ProjectCommand(
            name=name,
            slug=new_slug or slug,
            category=category,
            status=status,
            description=description,
            technologies_used=technologies,
            repository_url=repository_url,
            public_url=public_url,
            deployment_notes=deployment_notes,
            security_notes=security_notes,
            notes=notes,
        ),
        interface="mcp",
        actor="mcp-service-account",
        current_slug=slug,
        expected_updated_at=expected_updated_at,
    )


def sync_documentation(
    manifest: list[dict[str, Any]],
    update_existing: bool = True,
    report_orphans: bool = False,
    prune_orphans: bool = False,
    confirm_prune: bool = False,
) -> dict[str, Any]:
    """Synchronize a vault manifest into HQ; pruning requires explicit confirmation."""
    return documentation_service.sync_documentation(
        manifest,
        interface="mcp",
        actor="mcp-service-account",
        update_existing=update_existing,
        report_orphans=report_orphans,
        prune_orphans=prune_orphans,
        confirm_prune=confirm_prune,
    )


def _asset(asset: Asset) -> dict[str, Any]:
    return {
        "slug": asset.slug,
        "item_name": asset.item_name,
        "vendor": asset.vendor,
        "category": asset.category,
        "status": asset.status,
        "purchase_date": _iso(asset.purchase_date),
        "warranty_date": _iso(asset.warranty_date),
        "updated_at": _iso(asset.updated_at),
    }


def list_assets(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List HQ assets, optionally filtered by exact status or text search."""
    qs = Asset.objects.all()
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(
            Q(item_name__icontains=query)
            | Q(slug__icontains=query)
            | Q(vendor__icontains=query)
        )
    items = [_asset(asset) for asset in qs.order_by("slug")[: _page_size(limit)]]
    return {"items": items, "count": len(items)}


def get_asset(slug: str) -> dict[str, Any]:
    """Get one asset and its project, documentation, content, and expense links."""
    try:
        asset = Asset.objects.get(slug=slug)
    except Asset.DoesNotExist as exc:
        raise NotFoundError(f"Asset {slug!r} was not found.") from exc
    result = _asset(asset)
    result["relationships"] = {
        "projects": list(
            asset.related_projects.order_by("slug").values_list("slug", flat=True)
        ),
        "documentation": list(
            asset.documentation_records.filter(sensitivity__in=SAFE_SENSITIVITIES)
            .order_by("doc_id")
            .values_list("doc_id", flat=True)
        ),
        "content": list(
            asset.content_items.order_by("slug").values_list("slug", flat=True)
        ),
        "expense_ids": list(
            asset.expenses.order_by("-date", "-id").values_list("id", flat=True)
        ),
    }
    return result


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
