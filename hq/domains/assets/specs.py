"""The assets resource, declared on its domain in application/domains.py."""

from decimal import Decimal
from typing import Annotated, TypedDict

from django.db.models import Q

from hq.platform.application import assets, expenses
from hq.platform.application.answer_fields import (
    LinkedContent,
    LinkedDocs,
    LinkedExpenses,
    PageUrl,
    RecordSlug,
    UpdatedAt,
)
from hq.platform.application.business_use import BusinessUseShare
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import (
    DetailRead,
    ListFilter,
    ListRead,
    ListSort,
    ListToggle,
    ResourceSpec,
)
from hq.platform.application.record_fields import Notes
from hq.platform.application.resources import BoundedQuery, SearchText
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import ASSET_CATEGORY_CHOICES, Asset


class AssetQuery(BoundedQuery):
    query: SearchText = None


# Each order the list is read in, the first where none is asked.
SORTS = (
    *ListSort.both("-purchase_date", "Newest purchase", "Oldest purchase"),
    *ListSort.both("item_name", "Name"),
    *ListSort.both("vendor", "Vendor"),
    *ListSort.both("category", "Category"),
    *ListSort.both("total_cost", "Lowest cost", "Highest cost"),
    *ListSort.both("business_use_percentage", "Lowest business use", "Highest business use"),
    *ListSort.both("estimated_deductible_amount", "Lowest deductible", "Highest deductible"),
    *ListSort.both("status", "Status"),
)
FILTERS = (
    ListFilter("status", "Status", Asset.Status.choices),
    ListFilter("category", "Category", ASSET_CATEGORY_CHOICES),
)


def _unpriced() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """An active asset with no date or no cost, kept; a priced one, a free one and a retired one, left out."""

    undated = Asset.objects.create(item_name="Example router", total_cost=Decimal("40.00"))
    uncosted = Asset.objects.create(item_name="Example hub", purchase_date="2030-01-02")
    priced = Asset.objects.create(item_name="Example switch", purchase_date="2030-01-02", total_cost=Decimal("80.00"))
    free = Asset.objects.create(item_name="Example license", purchase_date="2030-01-02", total_cost=Decimal("0.00"))
    retired = Asset.objects.create(item_name="Example modem", status=Asset.Status.RETIRED)
    return (undated.slug, uncosted.slug), (priced.slug, free.slug, retired.slug)


UNPRICED = ListToggle(
    "missing_purchase",
    "No date or cost",
    "True for only the active assets with no purchase date or no cost recorded.",
    Q(status=Asset.Status.ACTIVE) & (Q(purchase_date__isnull=True) | Q(total_cost__isnull=True)),
    _unpriced,
)


class AssetRow(TypedDict):
    """One asset: what it is, what it cost and what it is to the business."""

    slug: RecordSlug
    url: PageUrl
    item_name: assets.AssetName
    vendor: assets.AssetVendor
    category: assets.AssetCategory
    status: assets.AssetStatus
    purchase_date: assets.PurchaseDate
    total_cost: assets.AssetCostAmount
    business_use_percentage: BusinessUseShare
    estimated_deductible_amount: assets.AssetDeductible
    payment_method: assets.PaymentMethod
    serial_number: assets.SerialNumber
    warranty_date: assets.WarrantyDate
    notes: Notes
    infrastructure: assets.AssetInfrastructure
    updated_at: UpdatedAt


class AssetLinks(TypedDict):
    """The records an asset is linked to."""

    projects: Annotated[list[str], Describe("The slugs of the projects this asset serves, sorted.")]
    content: LinkedContent
    documentation: LinkedDocs
    expense_ids: LinkedExpenses


class AssetRecord(AssetRow):
    """One asset with the records it is linked to."""

    relationships: AssetLinks


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "assets",
            "Assets",
            "Assets and their safe cross-domain relationships.",
            Capability.READ,
            listing=ListRead(
                assets.list_assets,
                AssetQuery,
                AssetRow,
                sorts=SORTS,
                filters=FILTERS,
                toggles=(UNPRICED,),
                totals=expenses.list_totals,
            ),
            detail=DetailRead(assets.get_asset, AssetRecord),
            identifier="slug",
            search=SearchDefinition.of(
                Asset,
                "slug",
                ("item_name", "slug", "vendor", "serial_number", "category", "notes"),
                title_field="item_name",
            ),
            web_route="assets:list",
        ),
    )
