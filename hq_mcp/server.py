"""Severino HQ MCP tool registration."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import services

mcp = FastMCP(
    "Severino HQ",
    instructions=(
        "Typed access to live Severino HQ operational data. "
        "Use the Vault MCP for runbook bodies and infrastructure procedures."
    ),
    stateless_http=True,
    json_response=True,
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
