"""Content commands shared by the web, MCP, and CLI adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from django.db import transaction

from assets.models import Asset
from content.models import ContentItem
from core.audit import operation_context
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from .security import Capability, Principal
from .projection import iso

SAFE_SENSITIVITIES = (
    DocumentationRecord.Sensitivity.PUBLIC,
    DocumentationRecord.Sensitivity.INTERNAL,
)


class NotFoundError(ValueError):
    """A content item or requested relationship does not exist."""


class ConflictError(ValueError):
    """A content item changed after the caller read it."""


@dataclass(frozen=True)
class ContentCommand:
    title: str
    slug: str = ""
    content_type: str = ContentItem.Type.ARTICLE
    status: str = ContentItem.Status.DRAFT
    topic: str = ""
    tags: str = ""
    published_url: str = ""
    wordpress_post_id: int | None = None
    wordpress_slug: str = ""
    published_at: date | None = None
    notes: str = ""
    related_projects: tuple[str, ...] = ()
    related_assets: tuple[str, ...] = ()
    related_expenses: tuple[int, ...] = ()
    related_documentation: tuple[str, ...] = ()




def serialize_content(item: ContentItem) -> dict[str, Any]:
    return {
        "slug": item.slug,
        "title": item.title,
        "content_type": item.content_type,
        "status": item.status,
        "topic": item.topic,
        "tags": item.tag_list,
        "published_url": item.published_url,
        "wordpress_post_id": item.wordpress_post_id,
        "wordpress_slug": item.wordpress_slug,
        "published_at": iso(item.published_at),
        "notes": item.notes,
        "updated_at": iso(item.updated_at),
        "relationships": {
            "projects": list(
                item.related_projects.order_by("slug").values_list("slug", flat=True)
            ),
            "assets": list(
                item.related_assets.order_by("slug").values_list("slug", flat=True)
            ),
            "expense_ids": list(
                item.related_expenses.order_by("id").values_list("id", flat=True)
            ),
            "documentation": list(
                item.related_documentation.filter(
                    sensitivity__in=SAFE_SENSITIVITIES
                )
                .order_by("doc_id")
                .values_list("doc_id", flat=True)
            ),
        },
    }


def _resolve(model, field: str, values: tuple, label: str):
    records = list(model.objects.filter(**{f"{field}__in": values}))
    found = {getattr(record, field) for record in records}
    missing = sorted(set(values) - found)
    if missing:
        raise NotFoundError(f"Related {label}(s) not found: {', '.join(map(str, missing))}")
    return records


@transaction.atomic
def save_content(
    command: ContentCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(Capability.WRITE_CONTENT)
    operation = "content.create" if current_slug is None else "content.update"
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        if current_slug is None:
            item = ContentItem()
            created = True
        else:
            try:
                item = ContentItem.objects.select_for_update().get(slug=current_slug)
            except ContentItem.DoesNotExist as exc:
                raise NotFoundError(
                    f"Content item {current_slug!r} was not found."
                ) from exc
            created = False
            if expected_updated_at and item.updated_at.isoformat() != expected_updated_at:
                raise ConflictError(
                    f"Content item {current_slug!r} changed after it was read."
                )

        values = asdict(command)
        projects = _resolve(
            Project, "slug", values.pop("related_projects"), "project"
        )
        assets = _resolve(Asset, "slug", values.pop("related_assets"), "asset")
        expenses = _resolve(Expense, "id", values.pop("related_expenses"), "expense")
        docs = _resolve(
            DocumentationRecord,
            "doc_id",
            values.pop("related_documentation"),
            "documentation record",
        )
        for field, value in values.items():
            setattr(item, field, value)
        item.full_clean()
        item.save()
        item.related_projects.set(projects)
        item.related_assets.set(assets)
        item.related_expenses.set(expenses)
        item.related_documentation.set(docs)

    return {"ok": True, "created": created, "content": serialize_content(item)}


def content_command_from_cleaned_data(data: dict[str, Any]) -> ContentCommand:
    scalar_fields = {
        field: data.get(field)
        for field in ContentCommand.__dataclass_fields__
        if not field.startswith("related_")
    }
    return ContentCommand(
        **scalar_fields,
        related_projects=tuple(row.slug for row in data["related_projects"]),
        related_assets=tuple(row.slug for row in data["related_assets"]),
        related_expenses=tuple(row.id for row in data["related_expenses"]),
        related_documentation=tuple(
            row.doc_id for row in data["related_documentation"]
        ),
    )
