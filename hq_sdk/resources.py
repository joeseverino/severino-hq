"""Discoverable read-resource contracts shared by HQ and its plugins."""

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
