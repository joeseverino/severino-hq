"""The resource registry as an extension's install leaves it, served to the MCP tools a test binds.

A test's registry stands in the integration graph itself, so every read of the
registry (a tool's schema, its read, the catalog) answers the one registry.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, TypedDict

from hq.platform.application.integration_specs import DetailRead, ListRead, ResourceSpec
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import held_graph
from hq.platform.application.projection import page_of
from hq.platform.application.resources import EmptyQuery, resource_registry

from ..binding import bind_tools
from ..server import ToolServer

KIND = "example.records"


class ExampleRecord(TypedDict):
    name: str
    state: str


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
        listing=ListRead(lambda: page_of([]), EmptyQuery, ExampleRecord),
        detail=DetailRead(detail),
        identifier="name",
    )


def with_kind(spec: ResourceSpec) -> dict[str, ResourceSpec]:
    """The host's registry with ``spec`` added."""

    return {**resource_registry(), spec.name: spec}


@contextmanager
def registry_patched(registry: dict[str, ResourceSpec]) -> Iterator[None]:
    """The composed graph with ``registry`` as its resources, for a tool a test binds itself."""

    with override_integration_graph(replace(held_graph(), resources=registry)):
        yield


@contextmanager
def served(registry: dict[str, ResourceSpec]) -> Iterator[ToolServer]:
    """A server whose tools are bound from ``registry``, and whose reads answer from it."""

    with registry_patched(registry):
        yield ToolServer(bind_tools())
