"""Thin MCP adapters over HQ's canonical application services and safe queries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Count
from django.utils import timezone

from application import assets as asset_service
from application import content as content_service
from application import documentation as documentation_service
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
    return _write(
        project_service.save_project,
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
    return _write(
        project_service.save_project,
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
    return _write(
        documentation_service.sync_documentation,
        manifest,
        update_existing=update_existing,
        report_orphans=report_orphans,
        prune_orphans=prune_orphans,
        confirm_prune=confirm_prune,
    )


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


def create_asset(
    item_name: str,
    slug: str = "",
    vendor: str = "",
    category: str = "other",
    purchase_date: date | None = None,
    total_cost: Decimal = Decimal("0.00"),
    business_use_percentage: int = 100,
    payment_method: str = "",
    serial_number: str = "",
    warranty_date: date | None = None,
    status: str = "active",
    notes: str = "",
    related_projects: list[str] | None = None,
) -> dict[str, Any]:
    """Create an HQ asset through the canonical validated service."""
    return _write(
        asset_service.save_asset,
        asset_service.AssetCommand(
            item_name=item_name,
            slug=slug,
            vendor=vendor,
            category=category,
            purchase_date=purchase_date,
            total_cost=total_cost,
            business_use_percentage=business_use_percentage,
            payment_method=payment_method,
            serial_number=serial_number,
            warranty_date=warranty_date,
            status=status,
            notes=notes,
            related_projects=tuple(related_projects or ()),
        ),
    )


def update_asset(
    slug: str,
    item_name: str,
    new_slug: str = "",
    vendor: str = "",
    category: str = "other",
    purchase_date: date | None = None,
    total_cost: Decimal = Decimal("0.00"),
    business_use_percentage: int = 100,
    payment_method: str = "",
    serial_number: str = "",
    warranty_date: date | None = None,
    status: str = "active",
    notes: str = "",
    related_projects: list[str] | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Update an HQ asset with optional optimistic concurrency protection."""
    return _write(
        asset_service.save_asset,
        asset_service.AssetCommand(
            item_name=item_name,
            slug=new_slug or slug,
            vendor=vendor,
            category=category,
            purchase_date=purchase_date,
            total_cost=total_cost,
            business_use_percentage=business_use_percentage,
            payment_method=payment_method,
            serial_number=serial_number,
            warranty_date=warranty_date,
            status=status,
            notes=notes,
            related_projects=tuple(related_projects or ()),
        ),
        current_slug=slug,
        expected_updated_at=expected_updated_at,
    )


def create_content(
    title: str,
    slug: str = "",
    content_type: str = "article",
    status: str = "draft",
    topic: str = "",
    tags: str = "",
    published_url: str = "",
    wordpress_post_id: int | None = None,
    wordpress_slug: str = "",
    published_at: date | None = None,
    notes: str = "",
    related_projects: list[str] | None = None,
    related_assets: list[str] | None = None,
    related_expenses: list[int] | None = None,
    related_documentation: list[str] | None = None,
) -> dict[str, Any]:
    """Create an HQ content item through the canonical service."""
    return _write(
        content_service.save_content,
        content_service.ContentCommand(
            title=title,
            slug=slug,
            content_type=content_type,
            status=status,
            topic=topic,
            tags=tags,
            published_url=published_url,
            wordpress_post_id=wordpress_post_id,
            wordpress_slug=wordpress_slug,
            published_at=published_at,
            notes=notes,
            related_projects=tuple(related_projects or ()),
            related_assets=tuple(related_assets or ()),
            related_expenses=tuple(related_expenses or ()),
            related_documentation=tuple(related_documentation or ()),
        ),
    )


def update_content(
    slug: str,
    title: str,
    new_slug: str = "",
    content_type: str = "article",
    status: str = "draft",
    topic: str = "",
    tags: str = "",
    published_url: str = "",
    wordpress_post_id: int | None = None,
    wordpress_slug: str = "",
    published_at: date | None = None,
    notes: str = "",
    related_projects: list[str] | None = None,
    related_assets: list[str] | None = None,
    related_expenses: list[int] | None = None,
    related_documentation: list[str] | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Update content with optional optimistic concurrency protection."""
    return _write(
        content_service.save_content,
        content_service.ContentCommand(
            title=title,
            slug=new_slug or slug,
            content_type=content_type,
            status=status,
            topic=topic,
            tags=tags,
            published_url=published_url,
            wordpress_post_id=wordpress_post_id,
            wordpress_slug=wordpress_slug,
            published_at=published_at,
            notes=notes,
            related_projects=tuple(related_projects or ()),
            related_assets=tuple(related_assets or ()),
            related_expenses=tuple(related_expenses or ()),
            related_documentation=tuple(related_documentation or ()),
        ),
        current_slug=slug,
        expected_updated_at=expected_updated_at,
    )


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
