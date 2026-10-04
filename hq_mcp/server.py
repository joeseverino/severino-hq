"""Severino HQ MCP tool registration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from hq_api.openapi import document

from . import services
from .contract import catalogue, resource_contract

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


# Snapshot the live deployment contract once, after Django has initialized.
_CONTRACT = resource_contract(document())
_LISTABLE = tuple(resource.name for resource in _CONTRACT.listable)
_ADDRESSABLE = tuple(resource.name for resource in _CONTRACT.addressable)

if TYPE_CHECKING:
    ListableResource = str
    AddressableResource = str
else:
    ListableResource = Literal[_LISTABLE]
    AddressableResource = Literal[_ADDRESSABLE]


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
    + catalogue(_CONTRACT.listable)
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
    + catalogue(_CONTRACT.addressable)
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
