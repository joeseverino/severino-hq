"""Severino HQ MCP tool registration."""

from __future__ import annotations

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

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


def register_read_tool(function):
    """Run synchronous Django ORM services on the thread-sensitive executor."""

    return mcp.tool()(sync_to_async(function, thread_sensitive=True))


register_read_tool(services.list_projects)
register_read_tool(services.get_project)
register_read_tool(services.list_assets)
register_read_tool(services.get_asset)
register_read_tool(services.list_expenses)
register_read_tool(services.list_receipts)
register_read_tool(services.documentation_status)
register_read_tool(services.recent_activity)
register_read_tool(services.system_health)
