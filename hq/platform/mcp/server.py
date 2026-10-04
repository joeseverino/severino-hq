"""Severino HQ MCP tool registration."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from hq.platform.api.openapi import document

from .binding import register_tools

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


# Snapshot the deployment contract after Django has initialized.
register_tools(mcp, document())
