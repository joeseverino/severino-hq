"""For exposing an extension's records to the API, MCP and the CLI as read resources with strict filters."""

from hq.platform.application.resources import (
    ResourceQuery,
    ResourceSpec,
    StatusFilter,
    TextFilter,
    TopQuery,
    describe_resources,
    get_resource,
    list_resource,
)

__all__ = [
    "ResourceQuery",
    "ResourceSpec",
    "StatusFilter",
    "TextFilter",
    "TopQuery",
    "describe_resources",
    "get_resource",
    "list_resource",
]
