"""Asset commands and queries shared by web, MCP, and CLI adapters."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import partial
from typing import Annotated, Any

from hq.domains.assets.models import ASSET_CATEGORY_CHOICES, PAYMENT_METHOD_CHOICES, Asset

from .answer_fields import BusinessUseShare
from .command_fields import Describe, Names, Refers
from .describe import listed
from .entity_links import EntityLink, entity_link
from .projection import addressable, iso, listing
from .record_fields import Notes, Slug, TotalCost
from .record_links import linked
from .record_saves import Held, Link, linked_saver, saved_type
from .security import Principal
from .ui import ended
from .upserts import upsert_by_slug
lazy from . import zones

# An asset's fields as its command writes them and its row reads them back, each said once.
type AssetName = Annotated[str, Describe("What the asset is called.")]
type AssetVendor = Annotated[str, Describe("Who it was bought from; empty when not recorded.")]
type AssetCategory = Annotated[
    str, Describe(ended(f"What type of asset it is: {listed(value for value, _ in ASSET_CATEGORY_CHOICES)}"))
]
type AssetStatus = Annotated[
    str, Describe(ended(f"Whether it is in use, or how it left: {listed(Asset.Status.values)}"))
]
type PurchaseDate = Annotated[date | None, Describe("When it was bought, an ISO 8601 date; null when not recorded.")]
type SerialNumber = Annotated[str, Describe("The maker's serial number; empty when not recorded.")]
type WarrantyDate = Annotated[
    date | None, Describe("When its warranty ends, an ISO 8601 date; null for none recorded.")
]
type AssetProjects = Annotated[tuple[str, ...], Names("projects"), Describe("The projects this asset serves.")]
type AssetInfrastructure = Annotated[
    str,
    Refers(Asset, "infrastructure"),
    Describe(
        "The machine, domain or certificate this asset is, as `type:key` such as `machine:lab-1`; empty for none."
    ),
]
# How an asset or an expense was paid, from the one set both choose from.
type PaymentMethod = Annotated[
    str,
    Describe(f"How it was paid: {listed(value for value, _ in PAYMENT_METHOD_CHOICES)}; empty when not recorded."),
]


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
    item_name: AssetName
    slug: Slug = ""
    vendor: AssetVendor = ""
    category: AssetCategory = "other"
    purchase_date: PurchaseDate = None
    total_cost: TotalCost = Decimal("0.00")
    business_use_percentage: BusinessUseShare = 100
    payment_method: PaymentMethod = ""
    serial_number: SerialNumber = ""
    warranty_date: WarrantyDate = None
    status: AssetStatus = Asset.Status.ACTIVE
    notes: Notes = ""
    related_projects: AssetProjects = ()
    infrastructure: AssetInfrastructure = ""


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


# What an asset's save answers, read off its record type (``saved_type``).
type AssetSaved = saved_type("assets")


def upsert_asset(
    command: AssetCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
    sent: frozenset[str] | None = None,
) -> AssetSaved:
    """Idempotently create or update an asset by its command slug."""

    return upsert_by_slug(
        Asset,
        command,
        save_asset,
        principal=principal,
        expected_updated_at=expected_updated_at,
        sent=sent,
    )
