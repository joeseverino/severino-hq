"""For exposing an extension's records to its pages, the API, MCP and the CLI as a list read with declared sorts, filters and toggles."""

from hq.platform.application.integration_specs import DetailRead, ListFilter, ListRead, ListSort, ListToggle
from hq.platform.application.projection import listing
from hq.platform.application.resources import (
    BoundedQuery,
    ResourceSpec,
    SearchText,
    describe_resources,
    get_resource,
    list_resource,
)

__all__ = [
    "BoundedQuery",
    "DetailRead",
    "ListFilter",
    "ListRead",
    "ListSort",
    "ListToggle",
    "ResourceSpec",
    "SearchText",
    "describe_resources",
    "get_resource",
    "list_resource",
    "listing",
]
