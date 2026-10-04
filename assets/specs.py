"""The assets resource, declared on its domain in application/domains.py."""

from __future__ import annotations

from application import assets
from application.integration_specs import ResourceSpec
from application.resources import BoundedQuery
from application.search_contracts import SearchDefinition
from application.security import Capability

from .models import Asset


class AssetQuery(BoundedQuery):
    status: str | None = None
    query: str | None = None


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
            not_found_errors=(assets.NotFoundError,),
            search=SearchDefinition(
                "assets",
                Asset,
                "slug",
                ("item_name", "slug", "vendor", "serial_number", "category", "notes"),
                label="Assets",
                title_field="item_name",
            ),
            web_route="assets:list",
        ),
    )
