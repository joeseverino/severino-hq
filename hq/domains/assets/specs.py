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
from hq.platform.application.describe import Describe, listed
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.record_fields import Notes
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

from .models import ASSET_CATEGORY_CHOICES, PAYMENT_METHOD_CHOICES, Asset

AssetQuery = create_model(
    "AssetQuery",
    __base__=BoundedQuery,
    status=(one_of(Asset.Status.values) | None, Field(default=None, description=STATUS_FILTER)),
    query=(TextFilter, None),
)


# How an asset or an expense was paid, from the one set both choose from.
type PaymentMethod = Annotated[
    str,
    Describe(f"How it was paid: {listed(value for value, _ in PAYMENT_METHOD_CHOICES)}; empty when not recorded."),
]


class AssetRow(TypedDict):
    """One asset: what it is, what it cost and what it is to the business."""

    slug: RecordSlug
    item_name: assets.AssetName
    vendor: Annotated[str, Describe("Who it was bought from; empty when not recorded.")]
    category: Annotated[
        str, Describe(ended(f"What type of asset it is: {listed(value for value, _ in ASSET_CATEGORY_CHOICES)}"))
    ]
    status: Annotated[str, Describe(ended(f"Whether it is in use, or how it left: {listed(Asset.Status.values)}"))]
    purchase_date: Annotated[str | None, Describe("When it was bought, an ISO 8601 date; null when not recorded.")]
    total_cost: TotalCostAmount
    business_use_percentage: BusinessUseShare
    estimated_deductible_amount: DeductibleAmount
    payment_method: PaymentMethod
    serial_number: Annotated[str, Describe("The maker's serial number; empty when not recorded.")]
    warranty_date: Annotated[str | None, Describe("When its warranty ends, an ISO 8601 date; null for none recorded.")]
    notes: Notes
    infrastructure: Annotated[
        str,
        Describe(
            "The machine, domain or certificate this asset is, as `type:key` such as `machine:lab-1`; empty for none."
        ),
    ]
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
