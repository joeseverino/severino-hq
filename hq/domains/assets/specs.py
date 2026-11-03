"""The assets resource, declared on its domain in application/domains.py."""

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
        ),
    )
