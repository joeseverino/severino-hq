"""The assets resource, declared on its domain in application/domains.py."""

from typing import TypedDict

from pydantic import Field, create_model

from hq.platform.application import assets
from hq.platform.application.integration_specs import ResourceSpec
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
    slug: str
    item_name: str
    vendor: str
    category: str
    status: str
    purchase_date: str | None
    # Money, as a decimal string.
    total_cost: str
    business_use_percentage: int
    estimated_deductible_amount: str
    payment_method: str
    serial_number: str
    warranty_date: str | None
    notes: str
    infrastructure: str
    updated_at: str


class AssetLinks(TypedDict):
    projects: list[str]
    content: list[str]
    documentation: list[str]
    expense_ids: list[int]


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
