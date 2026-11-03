"""The assets resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field, create_model

from hq.platform.application import assets
from hq.platform.application.answer_fields import (
    BusinessUseShare,
    DeductibleAmount,
    LinkedContent,
    LinkedDocs,
    LinkedExpenses,
    RecordSlug,
    TotalCostAmount,
    UpdatedAt,
)
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.record_fields import Notes
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import Asset

AssetQuery = create_model(
    "AssetQuery",
    __base__=BoundedQuery,
    status=(one_of(Asset.Status.values) | None, Field(default=None, description=STATUS_FILTER)),
    query=(TextFilter, None),
)


class AssetRow(TypedDict):
    """One asset: what it is, what it cost and what it is to the business."""

    slug: RecordSlug
    item_name: assets.AssetName
    vendor: assets.AssetVendor
    category: assets.AssetCategory
    status: assets.AssetStatus
    purchase_date: assets.PurchaseDate
    total_cost: TotalCostAmount
    business_use_percentage: BusinessUseShare
    estimated_deductible_amount: DeductibleAmount
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
            assets.list_assets,
            AssetQuery,
            assets.get_asset,
            "slug",
            search=SearchDefinition(
                "assets",
                Asset,
                "slug",
                ("item_name", "slug", "vendor", "serial_number", "category", "notes"),
                label="Assets",
                title_field="item_name",
            ),
            web_route="assets:list",
            row_type=AssetRow,
            record_type=AssetRecord,
        ),
    )
