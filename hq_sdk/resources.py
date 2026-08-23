"""Discoverable read-resource contracts shared by HQ and its plugins."""

from application.resources import (
    ResourceQuery,
    ResourceSpec,
    describe_resources,
    get_resource,
    list_resource,
    resource_registry,
)

__all__ = [
    "ResourceQuery",
    "ResourceSpec",
    "describe_resources",
    "get_resource",
    "list_resource",
    "resource_registry",
]
