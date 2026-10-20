"""Asset commands and queries shared by web, MCP, and CLI adapters."""

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project
from hq.platform.core.audit import operation_context

from .domains import records_of
from .entity_links import EntityLink, entity_link
from .projection import addressable, iso, listing
from .record_errors import RecordConflict, RecordNotFound
from .record_links import linked
from .security import Principal
from .upserts import upsert_by_slug
lazy from . import zones


def managed_domain(asset: Asset) -> EntityLink | None:
    """The domain HQ manages that a domain asset is, matched by name.

    An asset in the Domain category is named for the domain it registers, so
    its name is the join. None when the name is not a domain HQ knows.
    """

    if asset.category != "domain":
        return None
    name = asset.item_name.strip().lower()
    return entity_link("zone", name) if name in zones.zone_names() else None


@dataclass(frozen=True, slots=True)
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
    # ``kind:identity`` of the machine, domain or certificate this asset is.
    infrastructure: str = ""


def serialize_asset(asset: Asset, *, relationships: bool = False) -> dict[str, Any]:
    fields = {
        "slug": asset.slug,
        "item_name": asset.item_name,
        "vendor": asset.vendor,
        "category": asset.category,
        "status": asset.status,
        "purchase_date": iso(asset.purchase_date),
        "total_cost": str(asset.total_cost),
        "business_use_percentage": asset.business_use_percentage,
        "estimated_deductible_amount": str(asset.estimated_deductible_amount),
        "payment_method": asset.payment_method,
        "serial_number": asset.serial_number,
        "warranty_date": iso(asset.warranty_date),
        "notes": asset.notes,
        "infrastructure": asset.infrastructure,
        "updated_at": iso(asset.updated_at),
    }
    return linked(fields, asset, relationships=relationships, projects=asset.related_projects)


def list_assets(*, status: str | None = None, query: str | None = None, limit: int = 50) -> dict[str, Any]:
    return listing(
        Asset,
        serialize_asset,
        search=("item_name", "slug", "vendor"),
        status=status,
        query=query,
        limit=limit,
    )


def get_asset(slug: str) -> dict[str, Any]:
    return addressable(Asset, serialize_asset, slug)


@transaction.atomic
def save_asset(
    command: AssetCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(records_of("assets").write)
    operation = "asset.create" if current_slug is None else "asset.update"
    with operation_context(interface=principal.interface, actor=principal.actor, operation=operation):
        if current_slug is None:
            asset = Asset()
            created = True
        else:
            try:
                asset = Asset.objects.select_for_update().get(slug=current_slug)
            except Asset.DoesNotExist as exc:
                raise RecordNotFound("That asset was not found.") from exc
            created = False
            if expected_updated_at and asset.updated_at.isoformat() != expected_updated_at:
                raise RecordConflict("That asset changed after it was read.")

        values = asdict(command)
        project_slugs = values.pop("related_projects")
        projects = list(Project.objects.filter(slug__in=project_slugs))
        found_slugs = {project.slug for project in projects}
        missing = sorted(set(project_slugs) - found_slugs)
        if missing:
            raise RecordNotFound("A related project was not found.")

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


def upsert_asset(
    command: AssetCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Idempotently create or update an asset by its command slug."""

    return upsert_by_slug(
        Asset,
        command,
        save_asset,
        principal=principal,
        expected_updated_at=expected_updated_at,
    )
