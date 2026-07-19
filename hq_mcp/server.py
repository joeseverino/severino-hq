"""Severino HQ MCP tool registration."""

from __future__ import annotations

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

mcp.tool()(services.list_projects)
mcp.tool()(services.get_project)
mcp.tool()(services.list_assets)
mcp.tool()(services.get_asset)
mcp.tool()(services.list_expenses)
mcp.tool()(services.list_receipts)
mcp.tool()(services.documentation_status)
mcp.tool()(services.recent_activity)
mcp.tool()(services.system_health)
