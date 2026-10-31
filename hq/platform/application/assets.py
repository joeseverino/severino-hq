"""Asset commands and queries shared by web, MCP, and CLI adapters."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import partial
from typing import Annotated, Any

from hq.domains.assets.models import Asset

from .command_fields import Describe, Names, Refers
from .entity_links import EntityLink, entity_link
from .projection import addressable, iso, listing
from .record_links import linked
from .record_saves import Held, Link, linked_saver
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
    item_name: Annotated[str, Describe("What the asset is called.")]
    slug: Annotated[str, Describe("Its address in HQ; blank derives one from the name.")] = ""
    vendor: Annotated[str, Describe("Who it was bought from.")] = ""
    category: Annotated[str, Describe("What type of asset it is.")] = "other"
    purchase_date: Annotated[date | None, Describe("When it was bought.")] = None
    total_cost: Annotated[Decimal, Describe("What it cost in all.")] = Decimal("0.00")
    business_use_percentage: Annotated[int, Describe("How much of its use is for the business, as a percentage.")] = 100
    payment_method: Annotated[str, Describe("How it was paid for.")] = ""
    serial_number: Annotated[str, Describe("The maker's serial number.")] = ""
    warranty_date: Annotated[date | None, Describe("When its warranty ends.")] = None
    status: Annotated[str, Describe("Whether it is in use, or how it left.")] = Asset.Status.ACTIVE
    notes: Annotated[str, Describe("Anything else worth keeping.")] = ""
    related_projects: Annotated[tuple[str, ...], Names("projects"), Describe("The projects this asset serves.")] = ()
    infrastructure: Annotated[
        str,
        Refers(Asset, "infrastructure"),
        Describe("The machine, domain or certificate this asset is, as type:identity."),
    ] = ""


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


save_asset = linked_saver(
    "assets",
    partial(serialize_asset, relationships=True),
    refusals=Held("That asset was not found.", "That asset changed after it was read."),
    links={"related_projects": Link("slug", "A related project was not found.")},
)


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
