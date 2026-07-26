"""Canonical operating snapshot for HQ delivery adapters."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from assets.models import Asset
from contacts.d1 import D1Error, get_unread_count
from content.models import ContentItem
from control_plane.models import ManagedResource
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt

from .infrastructure import resource_health
from .read_models import recent_activity

ZERO_MONEY = Decimal("0.00")


def _project(project: Project) -> dict[str, Any]:
    return {
        "slug": project.slug,
        "name": project.name,
        "category": project.category,
        "category_label": project.get_category_display(),
        "repository_url": project.repository_url,
        "public_url": project.public_url,
        "updated_at": project.updated_at.isoformat(),
    }


def _content(item: ContentItem) -> dict[str, Any]:
    return {
        "slug": item.slug,
        "title": item.title,
        "content_type_label": item.get_content_type_display(),
        "published_url": item.published_url,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "updated_at": item.updated_at.isoformat(),
    }


def _documentation(record: DocumentationRecord) -> dict[str, Any]:
    return {
        "doc_id": record.doc_id,
        "title": record.title,
        "last_reviewed": record.last_reviewed.isoformat() if record.last_reviewed else None,
    }


def operating_snapshot() -> dict[str, Any]:
    """Return the one canonical KPI, work-queue, and activity projection."""
    try:
        unread_contacts_count = get_unread_count()
        contacts_status = "ok"
    except D1Error:
        unread_contacts_count = 0
        contacts_status = "unavailable"
    today = timezone.localdate()
    year_start = today.replace(month=1, day=1)
    expenses = Expense.objects.filter(date__gte=year_start).aggregate(
        total=Sum("total_cost"),
        deductible=Sum("estimated_deductible_amount"),
        count=Count("id"),
    )

    review_days = getattr(settings, "SEVERINO_DOC_REVIEW_INTERVAL_DAYS", 180)
    review_cutoff = today - timedelta(days=review_days)
    docs_needing_review = DocumentationRecord.objects.filter(
        Q(last_reviewed__isnull=True) | Q(last_reviewed__lt=review_cutoff),
        status=DocumentationRecord.Status.ACTIVE,
    ).exclude(doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT)

    active_projects = Project.objects.filter(status=Project.Status.ACTIVE)
    project_health = active_projects.annotate(
        content_count=Count("content_items", distinct=True),
        doc_count=Count("documentation_records", distinct=True),
    )
    projects_needing_output = project_health.filter(
        Q(content_count=0) | Q(doc_count=0)
    )
    published_content = ContentItem.objects.filter(
        status=ContentItem.Status.PUBLISHED
    )
    draft_content = ContentItem.objects.filter(status=ContentItem.Status.DRAFT)

    receipts_unlinked = Receipt.objects.filter(
        related_expense__isnull=True,
        related_asset__isnull=True,
    ).count()
    expenses_without_receipts = (
        Expense.objects.annotate(receipt_count=Count("receipts"))
        .filter(receipt_count=0)
        .count()
    )
    assets_missing_purchase = (
        Asset.objects.filter(status=Asset.Status.ACTIVE)
        .filter(Q(purchase_date__isnull=True) | Q(total_cost=0))
        .count()
    )
    content_without_docs = (
        ContentItem.objects.annotate(doc_count=Count("related_documentation"))
        .filter(doc_count=0)
        .count()
    )

    priority = [
        *[
            {
                "code": "infrastructure",
                "resource_key": resource.key,
                "label": (
                    f"{resource.key}: {health['message']}"
                    if health["message"]
                    else f"{resource.key} needs infrastructure attention"
                ),
                "count": 1,
                "severity": "critical" if health["state"] == "degraded" else "warning",
            }
            for resource in ManagedResource.objects.filter(enabled=True)
            if (health := resource_health(resource))["state"]
            in {"degraded", "pending", "unknown"}
        ],
        {"code": "docs_review", "label": "Docs need review", "count": docs_needing_review.count()},
        {"code": "draft_content", "label": "Draft content", "count": draft_content.count()},
        {"code": "unread_contacts", "label": "Unread contact submissions", "count": unread_contacts_count},
        {"code": "projects_output", "label": "Active projects need output", "count": projects_needing_output.count()},
        {"code": "receipts_unlinked", "label": "Receipts need links", "count": receipts_unlinked},
        {"code": "expenses_receipts", "label": "Expenses need receipts", "count": expenses_without_receipts},
        {"code": "assets_purchase", "label": "Assets missing purchase info", "count": assets_missing_purchase},
        {"code": "content_docs", "label": "Content needs docs", "count": content_without_docs},
    ]

    return {
        "generated_at": timezone.now().isoformat(),
        "upstreams": {"contacts": contacts_status},
        "year": today.year,
        "kpis": {
            "active_projects": active_projects.count(),
            "projects_needing_output": projects_needing_output.count(),
            "draft_content": draft_content.count(),
            "published_content": published_content.count(),
            "docs_needing_review": docs_needing_review.count(),
            "expenses_total": str(expenses["total"] or ZERO_MONEY),
            "expenses_count": expenses["count"] or 0,
            "deductible_total": str(expenses["deductible"] or ZERO_MONEY),
        },
        "priority": priority,
        "priority_count": sum(item["count"] for item in priority),
        "priority_group_count": sum(bool(item["count"]) for item in priority),
        "active_projects": [
            _project(project) for project in active_projects.order_by("-updated_at")[:4]
        ],
        "draft_content": [
            _content(item) for item in draft_content.order_by("-updated_at")[:4]
        ],
        "recent_published": [
            _content(item)
            for item in published_content.order_by("-published_at", "-updated_at")[:4]
        ],
        "docs_needing_review": [
            _documentation(record)
            for record in docs_needing_review.order_by("last_reviewed")[:4]
        ],
        "recent_activity": recent_activity(limit=8)["items"],
    }
