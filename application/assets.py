"""Asset commands and queries shared by web, MCP, and CLI adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q

from assets.models import Asset
from core.audit import operation_context
from docs_index.models import DocumentationRecord
from projects.models import Project
from .security import Capability, Principal

MAX_PAGE_SIZE = 100
SAFE_SENSITIVITIES = (
    DocumentationRecord.Sensitivity.PUBLIC,
    DocumentationRecord.Sensitivity.INTERNAL,
)


class NotFoundError(ValueError):
    """A requested asset or related registry object does not exist."""


class ConflictError(ValueError):
    """The caller tried to write over a newer version of an asset."""


@dataclass(frozen=True)
class AssetCommand:
    item_name: str
    slug: str = ""
    vendor: str = ""
    category: str = "other"
    purchase_date: date | None = None
    total_cost: Decimal = Decimal("0.00")
    business_use_percentage: int = 100
    payment_method: str = ""
    serial_number: str = ""
    warranty_date: date | None = None
    status: str = Asset.Status.ACTIVE
    notes: str = ""
    related_projects: tuple[str, ...] = ()


def _page_size(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_SIZE)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def serialize_asset(asset: Asset, *, relationships: bool = False) -> dict[str, Any]:
    result = {
        "slug": asset.slug,
        "item_name": asset.item_name,
        "vendor": asset.vendor,
        "category": asset.category,
        "status": asset.status,
        "purchase_date": _iso(asset.purchase_date),
        "total_cost": str(asset.total_cost),
        "business_use_percentage": asset.business_use_percentage,
        "estimated_deductible_amount": str(asset.estimated_deductible_amount),
        "payment_method": asset.payment_method,
        "serial_number": asset.serial_number,
        "warranty_date": _iso(asset.warranty_date),
        "notes": asset.notes,
        "updated_at": _iso(asset.updated_at),
    }
    if relationships:
        result["relationships"] = {
            "projects": list(
                asset.related_projects.order_by("slug").values_list("slug", flat=True)
            ),
            "documentation": list(
                asset.documentation_records.filter(
                    sensitivity__in=SAFE_SENSITIVITIES
                )
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


def list_assets(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    qs = Asset.objects.all()
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(
            Q(item_name__icontains=query)
            | Q(slug__icontains=query)
            | Q(vendor__icontains=query)
        )
    items = [
        serialize_asset(asset) for asset in qs.order_by("slug")[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def get_asset(slug: str) -> dict[str, Any]:
    try:
        asset = Asset.objects.get(slug=slug)
    except Asset.DoesNotExist as exc:
        raise NotFoundError(f"Asset {slug!r} was not found.") from exc
    return serialize_asset(asset, relationships=True)


@transaction.atomic
def save_asset(
    command: AssetCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(Capability.WRITE_ASSETS)
    operation = "asset.create" if current_slug is None else "asset.update"
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        if current_slug is None:
            asset = Asset()
            created = True
        else:
            try:
                asset = Asset.objects.select_for_update().get(slug=current_slug)
            except Asset.DoesNotExist as exc:
                raise NotFoundError(f"Asset {current_slug!r} was not found.") from exc
            created = False
            if expected_updated_at and asset.updated_at.isoformat() != expected_updated_at:
                raise ConflictError(
                    f"Asset {current_slug!r} changed after it was read."
                )

        values = asdict(command)
        project_slugs = values.pop("related_projects")
        projects = list(Project.objects.filter(slug__in=project_slugs))
        found_slugs = {project.slug for project in projects}
        missing = sorted(set(project_slugs) - found_slugs)
        if missing:
            raise NotFoundError(f"Related project(s) not found: {', '.join(missing)}")

        for field, value in values.items():
            setattr(asset, field, value)
        asset.full_clean()
        asset.save()
        asset.related_projects.set(projects)

    return {
        "ok": True,
        "created": created,
        "asset": serialize_asset(asset, relationships=True),
    }


@transaction.atomic
def upsert_asset(
    command: AssetCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Idempotently create or update an asset by its command slug."""

    current_slug = (
        command.slug
        if Asset.objects.select_for_update().filter(slug=command.slug).exists()
        else None
    )
    return save_asset(
        command,
        principal=principal,
        current_slug=current_slug,
        expected_updated_at=expected_updated_at,
    )


def asset_command_from_cleaned_data(data: dict[str, Any]) -> AssetCommand:
    related = data.get("related_projects") or ()
    values = {
        field: data.get(field)
        for field in AssetCommand.__dataclass_fields__
        if field != "related_projects"
    }
    return AssetCommand(
        **values,
        related_projects=tuple(project.slug for project in related),
    )
