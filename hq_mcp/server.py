"""Severino HQ MCP tool registration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from application.integration_specs import ResourceSpec
from application.resources import resource_registry

from . import services

mcp = FastMCP(
    "Severino HQ",
    instructions=(
        "Typed access to live Severino HQ operational data. "
        "Use the Vault MCP for runbook bodies and infrastructure procedures."
    ),
    stateless_http=True,
    json_response=True,
    # MCPBoundary owns Host and Origin enforcement before requests reach the
    # SDK. Keeping the SDK's localhost-only defaults would reject the explicit
    # Tailscale allowlist with 421 before tool dispatch.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)
mcp.settings.streamable_http_path = "/"


def register_tool(function: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    """Run synchronous Django ORM services on the thread-sensitive executor."""

    return mcp.tool()(sync_to_async(function, thread_sensitive=True))


# Resource kinds come from HQ's resource registry, so a kind a provider adds is
# readable here without touching this module.
_REGISTRY = resource_registry()


def _kinds(supported: Callable[[ResourceSpec], bool]) -> tuple[str, ...]:
    return tuple(sorted(name for name, spec in _REGISTRY.items() if supported(spec)))


_LISTABLE = _kinds(lambda spec: bool(spec.list_handler and spec.list_query_type))
_ADDRESSABLE = _kinds(lambda spec: bool(spec.detail_handler and spec.identifier))

if TYPE_CHECKING:
    ListableResource = str
    AddressableResource = str
else:
    ListableResource = Literal[_LISTABLE]
    AddressableResource = Literal[_ADDRESSABLE]


def _catalogue(kinds: tuple[str, ...], *, identifier: bool) -> str:
    lines = []
    for name in kinds:
        spec = _REGISTRY[name]
        key = f" Identifier: `{spec.identifier}`." if identifier else ""
        lines.append(f"- `{name}`: {spec.summary}{key}")
    return "\n".join(lines)


def list_resource(
    name: Annotated[ListableResource, Field(description="The resource kind to list.")],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Filters for this kind, validated strictly against its query schema "
                "(describe_resources returns each schema). Unknown or mistyped "
                "filters are rejected."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    return services.list_resource(name, filters)


list_resource.__doc__ = (
    "List records of one HQ resource kind as `{items, count}`. Pages are bounded; "
    "a kind may refuse a caller without the capability it requires. Kinds:\n"
    + _catalogue(_LISTABLE, identifier=False)
)


def get_resource(
    name: Annotated[
        AddressableResource, Field(description="The resource kind the record belongs to.")
    ],
    identifier: Annotated[
        str | int, Field(description="The record's identifier, as named for its kind below.")
    ],
) -> dict[str, Any]:
    return services.get_resource(name, identifier)


get_resource.__doc__ = (
    "Get one record of an HQ resource kind, with its relationships. A missing record "
    "is an error, not an empty result. Kinds and their identifiers:\n"
    + _catalogue(_ADDRESSABLE, identifier=True)
)


register_tool(services.describe_capabilities)
register_tool(services.execute_capability)
register_tool(services.describe_resources)
register_tool(services.describe_connections)
register_tool(services.list_connections)
register_tool(services.get_topology)
register_tool(services.get_findings)
register_tool(list_resource)
register_tool(get_resource)
register_tool(services.audit_registry)
register_tool(services.export_year_summary)
register_tool(services.documentation_status)
register_tool(services.recent_activity)
register_tool(services.system_health)
register_tool(services.dashboard_snapshot)
