"""Severino HQ's MCP server: the tools the API document declares, on the SDK's low-level server.

HQ owns each tool's schema (emitted into the document) and its validation, so
the server is given them rather than deriving its own from signatures: the
SDK lists, dispatches and frames, and nothing here reaches into its internals.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import TypeAdapter
from starlette.applications import Starlette

from hq.platform.api.openapi import document

from .binding import Tool, bind_tools

# An answer is any value a tool returns, serialized by what it is.
_ANSWER = TypeAdapter(Any)


def _answer(result: Any) -> types.CallToolResult:
    """An answer as a client reads it: the object, and the same as indented JSON text."""

    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text", text=_ANSWER.dump_json(result, by_alias=True, fallback=str, indent=2).decode()
            )
        ],
        structured_content=_ANSWER.dump_python(result, mode="json", by_alias=True, fallback=str),
        is_error=False,
    )


def _refusal(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


class ToolServer:
    """The declared tools, served over stateless streamable HTTP with JSON answers."""

    def __init__(self, tools: Mapping[str, Tool]) -> None:
        self.tools = tools
        self.server: Server[Any] = Server(
            "Severino HQ",
            instructions=(
                "Typed access to live Severino HQ operational data. "
                "Use the Vault MCP for runbook bodies and infrastructure procedures."
            ),
            get_tool_input_schema=self._input_schema,
            on_list_tools=self._list,
            on_call_tool=self._call,
        )

    def _input_schema(self, name: str) -> Mapping[str, Any] | None:
        tool = self.tools.get(name)
        return None if tool is None else tool.input_schema

    async def _list(self, _context: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema),
                    output_schema=tool.output_schema,
                    annotations=types.ToolAnnotations.model_validate(dict(tool.annotations)),
                )
                for tool in self.tools.values()
            ]
        )

    async def _call(self, _context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = self.tools.get(params.name)
        if tool is None:
            return _refusal(f"Unknown tool: {params.name}")
        try:
            result = await tool.call(params.arguments or {})
        except Exception as error:  # noqa: BLE001 - a tool's failure is its answer to the agent
            return _refusal(f"Error executing tool {tool.name}: {error}")
        return _answer(result)

    def served(self) -> Served:
        """A fresh application and the session manager it runs on, as a pair:
        each is started once, so whoever mounts the one runs the other."""

        application = self.server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            # MCPBoundary owns Host and Origin enforcement before requests reach
            # the SDK. Keeping the SDK's localhost-only defaults would reject the
            # explicit Tailscale allowlist with 421 before tool dispatch.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        return Served(application, self.server.session_manager)


@dataclass(frozen=True, slots=True)
class Served:
    application: Starlette
    sessions: StreamableHTTPSessionManager


# Snapshot the deployment contract after Django has initialized.
mcp = ToolServer(bind_tools(document()))
