"""The resource registry as an extension's install leaves it, served to the MCP tools a test binds.

The tools' schemas are bound from the contract's view of the registry, and
their reads answered from the application's, so a kind a test adds is served
only when both names answer it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import EmptyQuery, resource_registry

from ..binding import bind_tools
from ..server import ToolServer

KIND = "example.records"
# What reads answer from, and what the tools' schemas are bound from.
_READ = "hq.platform.application.resources.resource_registry"
_BOUND = "hq.platform.mcp.contract.resource_registry"


def example_kind(seen: list[str] | None = None) -> ResourceSpec:
    """A kind the host does not name, read under a capability a plain reader does not hold.

    Each identifier its detail is read for is appended to ``seen``.
    """

    def detail(identifier: str) -> dict[str, Any]:
        if seen is not None:
            seen.append(identifier)
        return {"name": identifier, "state": "ok"}

    return ResourceSpec(
        KIND,
        "Example records",
        "Records an extension declares.",
        "example_read",
        lambda: {"items": [], "count": 0},
        EmptyQuery,
        detail,
        "name",
    )


def with_kind(spec: ResourceSpec) -> dict[str, ResourceSpec]:
    """The host's registry with ``spec`` added."""

    return {**resource_registry(), spec.name: spec}


@contextmanager
def registry_patched(registry: dict[str, ResourceSpec]) -> Iterator[None]:
    """Both names answer ``registry``, for a tool a test binds itself."""

    with mock.patch(_READ, return_value=registry), mock.patch(_BOUND, return_value=registry):
        yield


@contextmanager
def served(registry: dict[str, ResourceSpec]) -> Iterator[ToolServer]:
    """A server whose tools are bound from ``registry``, and whose reads answer from it."""

    with registry_patched(registry):
        yield ToolServer(bind_tools())
