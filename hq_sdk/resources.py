"""For exposing an extension's records to the API, MCP and the CLI as read resources with strict filters."""

from hq.platform.application.integration_specs import ListRead
from hq.platform.application.resources import (
    ResourceSpec,
    StatusFilter,
    TextFilter,
    TopQuery,
    describe_resources,
    get_resource,
    list_resource,
)

__all__ = [
    "ListRead",
    "ResourceSpec",
    "StatusFilter",
    "TextFilter",
    "TopQuery",
    "describe_resources",
    "get_resource",
    "list_resource",
]
