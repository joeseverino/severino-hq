"""Severino HQ's MCP server, on the SDK's low-level server: the declared tools,
the prompts and resources a person reaches through an agent's client, and the
tasks extension over held changes.

HQ owns each tool's schema (the API document carries the same) and its validation, so
the server is given them rather than deriving its own from signatures: the
SDK lists, dispatches and frames, and nothing here reaches into its internals.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from asgiref.sync import sync_to_async
from mcp import types
from mcp.server import CacheHint
from mcp.server.caching import CacheableMethod
from mcp.server.lowlevel import Server
from mcp.server.mcpserver.server import require_client_extension
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter
from starlette.applications import Starlette

from hq.platform.application import agent_admission, approvals
from hq.platform.application.capabilities import adapter_refusal
from hq.platform.application.refusal_status import LATER_CODES, Refusal, refusal_of
from hq.platform.application.security import Principal

from . import prompts, resources, tasks
from .binding import Tool, bind_tools
from .contract import ResourceContract, served_resources
from .identity import authenticated, caller_address, current_principal

# An answer is any value a tool returns, serialized by what it is.
_ANSWER = TypeAdapter(Any)
# The key a refusal's wait, in whole seconds, rides under outside the answer itself.
RETRY_AFTER = "retry_after"

# What a third-party agent needs before its first call, a paragraph per topic,
# kept well inside the 4,096 characters a client loads up front.
INSTRUCTIONS = "\n\n".join(
    (
        (
            "Typed access to Severino HQ's live operational records. describe_resources names what "
            "list_resource and get_resource read; describe_capabilities, narrowed by name or resource, "
            "names what execute_capability runs."
        ),
        "An answer is JSON. A refusal has ok: false and error.code; a held change is not one.",
        (
            'A change run through execute_capability may answer status: "awaiting_approval": nothing was '
            "written, a person decides it in HQ's web interface, and repeating the call returns the same "
            "request, so report approval.id to whoever asked and stop."
        ),
        (
            "Every capability whose effect is not read takes an idempotency_key in its payload: send one, "
            "and reuse it when you retry, so a repeat returns the first result instead of acting twice."
        ),
        (
            "While an operator has paused agents, every tool call answers error.code agents_paused and "
            "does nothing; the connection stays up, so retry the call later, without reconnecting. "
            f"error.code {' or '.join(sorted(LATER_CODES))} means HQ takes no more of this agent's calls "
            "for now: nothing ran, so wait error.retry_after seconds and retry. call_timed_out means HQ "
            "stopped waiting and the call may still finish: read before repeating it, and repeat a change "
            "with the same idempotency_key."
        ),
        (
            "Prompts needs-me, status-brief and held-requests each return one message of HQ's live answer "
            "for a person to act on. Resources hq://action_items, hq://approvals and hq://hq are the "
            "action queue, the held requests and HQ itself; a template hq://<kind>/{identifier} reads one "
            "record as get_resource does. hq://approvals, the held-requests prompt and the action queue "
            "show you only the held requests you asked for yourself; a request another agent holds is "
            "not yours to read or to wait on."
        ),
        (
            "A client that declares the io.modelcontextprotocol/tasks extension is answered a held change "
            "as a task whose id is approval.id: tasks/get reads working until a person decides, then "
            "completed with the call's answer, or with an error result coded declined, expired or stale. "
            "Poll at pollIntervalMs; neither tasks/update nor tasks/cancel decides it, and a task answers "
            "only the agent that asked."
        ),
    )
)


# How long a client may keep a list. The tools, prompts and fixed views change
# only with a release, and are the same for every caller, though only one
# signed in reaches them, so private; the resource templates are the caller's
# grants and records, so they are asked for afresh.
STATIC_FOR = 10 * 60 * 1000
CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=STATIC_FOR, scope="private"),
    "prompts/list": CacheHint(ttl_ms=STATIC_FOR, scope="private"),
    "resources/list": CacheHint(ttl_ms=STATIC_FOR, scope="private"),
    "resources/templates/list": CacheHint(ttl_ms=0, scope="private"),
}


def _answer(result: Any) -> types.CallToolResult:
    """An answer as a client reads it: the object, and the same as compact JSON text.

    A refusal (``ok`` false) is a tool error. A held change is ``ok`` with
    ``status: "awaiting_approval"``, and stays a success: its status, not
    ``isError``, is what a client keys on. A refusal is answered as the
    ``Refusal`` it stands for, so a code the contract does not hold reaches
    the client as ``operation_failed``, as it does from every other door.
    """

    refusal = refusal_of(result) if isinstance(result, dict) and result.get("ok") is False else None
    if refusal is not None:
        result = refusal.answer()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_ANSWER.dump_json(result, by_alias=True, fallback=str).decode())],
        structured_content=_ANSWER.dump_python(result, mode="json", by_alias=True, fallback=str),
        is_error=refusal is not None,
        _meta=None if refusal is None else _wait(refusal),
    )


def _wait(refusal: Refusal) -> dict[str, Any] | None:
    """A refusal's wait where a client reads it without parsing the answer:
    ``_meta`` on a tool result, ``data`` on a protocol error."""

    return None if refusal.retry_after is None else {RETRY_AFTER: refusal.retry_after}


# The statuses that say what the request names or sends is wrong; any other
# refusal below 500 is the request's standing (a denial, a pause, a conflict).
_PARAMS_STATUS = frozenset({400, 404, 405, 413, 415})


def _protocol_error(status: int) -> int:
    """The protocol error a refusal is outside a tool call, read from the HTTP status its code has everywhere."""

    if status >= 500:
        return types.INTERNAL_ERROR
    return types.INVALID_PARAMS if status in _PARAMS_STATUS else types.INVALID_REQUEST


def _as_protocol_error(refused: Refusal) -> MCPError:
    """A refusal as a protocol error, HQ's own code and any wait riding in its data."""

    return MCPError(
        _protocol_error(refused.status), refused.message, data={"code": refused.code, **(_wait(refused) or {})}
    )


def _refused_now() -> Refusal | None:
    """The brake on this call's caller, or None when it may act."""

    refusal = agent_admission.refused(current_principal(), source=caller_address())
    return None if refusal is None else refusal.refusal


async def _read[T](name: str, read: Callable[[Principal], T], *, braked: bool = True) -> T:
    """``read`` as this request's caller, under the brake a tool call is under
    unless it is a list, which a paused agent still reads as it lists tools.

    Outside a tool call there is no result to carry a refusal, so it is the
    protocol error ``_as_protocol_error`` names, in HQ's own words.
    """

    def run() -> T:
        try:
            refused = _refused_now() if braked else None
            if refused is None:
                return read(current_principal())
        except MCPError:
            raise
        except Exception as error:  # noqa: BLE001 - a read's failure is its answer to the agent
            raise _as_protocol_error(refusal_of(error, name=name, principal=authenticated())) from None
        raise _as_protocol_error(refused)

    return await sync_to_async(run)()


@lru_cache(maxsize=1)
def _bound(_resources: ResourceContract) -> dict[str, Tool]:
    return bind_tools()


def composed_tools() -> dict[str, Tool]:
    """The tools as the composition in force declares them, bound once while it stands.

    Asked at each call rather than at import: imports are lazy, so the first
    use of the server can fall in any composition, and a resource tool's
    ``name`` takes only the kinds its binding saw.
    """

    return _bound(served_resources())


class ToolServer:
    """The declared tools, prompts, resources and tasks, served over stateless streamable HTTP with JSON answers."""

    def __init__(self, tools: Mapping[str, Tool] | None = None) -> None:
        # A fixed set for a probe; left out, the composition's at each call.
        self._fixed = tools
        self.server: Server[Any] = Server(
            "Severino HQ",
            instructions=INSTRUCTIONS,
            cache_hints=CACHE_HINTS,
            get_tool_input_schema=self._input_schema,
            on_list_tools=self._list,
            on_list_prompts=self._list_prompts,
            on_get_prompt=self._get_prompt,
            on_list_resources=self._list_resources,
            on_list_resource_templates=self._list_templates,
            on_read_resource=self._read_resource,
            on_completion=self._complete,
        )
        # Registered by method, as the SDK registers a tools/call an extension
        # wraps: a task is a result outside what on_call_tool is typed to return.
        self.server.add_request_handler("tools/call", types.CallToolRequestParams, self._call)
        # The lowlevel server's way to serve an extension: its methods, and the
        # identifier advertised in the server's capabilities.
        self.server.extensions[tasks.IDENTIFIER] = {}
        self.server.add_request_handler("tasks/get", types.GetTaskRequestParams, self._get_task)
        self.server.add_request_handler("tasks/update", tasks.UpdateTaskRequestParams, self._update_task)
        self.server.add_request_handler("tasks/cancel", types.CancelTaskRequestParams, self._cancel_task)

    @property
    def tools(self) -> Mapping[str, Tool]:
        return composed_tools() if self._fixed is None else self._fixed

    def _input_schema(self, name: str) -> Mapping[str, Any] | None:
        tool = self.tools.get(name)
        return None if tool is None else tool.input_schema

    async def _list(self, _context: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.name,
                    title=tool.title or None,
                    description=tool.description,
                    input_schema=dict(tool.input_schema),
                    output_schema=None if tool.output_schema is None else dict(tool.output_schema),
                    annotations=types.ToolAnnotations.model_validate(dict(tool.annotations)),
                )
                for tool in self.tools.values()
            ]
        )

    async def _call(self, context: Any, params: types.CallToolRequestParams) -> types.CallToolResult | dict[str, Any]:
        tool = self.tools.get(params.name)
        if tool is None:
            # A protocol error, as the MCP spec has it, since no tool ran.
            raise MCPError(
                types.INVALID_PARAMS,
                f"Unknown tool: {params.name}. tools/list names each tool.",
                data={"next": {"method": "tools/list"}},
            )
        try:
            # Asked at each call rather than at the door, so a paused agent
            # keeps its connection and the next call after a resume runs. A
            # brake that cannot be read refuses, through the failure below.
            refused = await sync_to_async(_refused_now)()
            if refused is not None:
                return _answer(refused.answer())
            result = await tool.call(params.arguments or {})
            if isinstance(result, dict) and approvals.is_held(result) and tasks.requested(context):
                task = await sync_to_async(tasks.created)(result, current_principal())
                if task is not None:
                    return task
        except Exception as error:  # noqa: BLE001 - a tool's failure is its answer to the agent
            # On a thread, since a denial is written to the audit log as it is mapped.
            refusal = await sync_to_async(adapter_refusal)(tool.name, error, principal=authenticated())
            return _answer(refusal)
        return _answer(result)

    async def _list_prompts(self, _context: Any, _params: Any) -> types.ListPromptsResult:
        return prompts.listed()

    async def _get_prompt(self, _context: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        return await _read(f"prompt {params.name}", lambda principal: prompts.message(params.name, principal))

    async def _list_resources(self, _context: Any, _params: Any) -> types.ListResourcesResult:
        return resources.listed()

    async def _list_templates(self, _context: Any, _params: Any) -> types.ListResourceTemplatesResult:
        return await _read("resources/templates/list", resources.templates, braked=False)

    async def _read_resource(self, _context: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        found = await _read(f"resource {params.uri}", lambda principal: resources.read(params.uri, principal))
        text = _ANSWER.dump_json(found, by_alias=True, fallback=str).decode()
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, mime_type=resources.MIME, text=text)]
        )

    async def _complete(self, _context: Any, params: types.CompleteRequestParams) -> types.CompleteResult:
        ref = params.ref
        if not isinstance(ref, types.ResourceTemplateReference):
            # No prompt takes an argument, so a prompt has nothing to complete.
            return types.CompleteResult(completion=types.Completion(values=[]))
        argument = params.argument
        completion = await _read(
            "completion/complete",
            lambda principal: resources.completed(ref.uri, argument.name, argument.value, principal),
        )
        return types.CompleteResult(completion=completion)

    async def _get_task(self, context: Any, params: types.GetTaskRequestParams) -> dict[str, Any]:
        require_client_extension(context, tasks.IDENTIFIER)
        task, answer = await _read("tasks/get", lambda principal: tasks.state(params.task_id, principal))
        if answer is not None:
            task["result"] = _answer(answer).model_dump(mode="json", by_alias=True, exclude_none=True)
        return task

    async def _update_task(self, context: Any, params: tasks.UpdateTaskRequestParams) -> dict[str, Any]:
        require_client_extension(context, tasks.IDENTIFIER)
        return await _read("tasks/update", lambda principal: tasks.acknowledge(params.task_id, principal))

    async def _cancel_task(self, context: Any, params: types.CancelTaskRequestParams) -> dict[str, Any]:
        require_client_extension(context, tasks.IDENTIFIER)
        return await _read("tasks/cancel", lambda principal: tasks.refuse_cancel(params.task_id, principal))

    def served(self) -> Served:
        """A fresh application and the session manager it runs on, as a pair:
        each is started once, so whoever mounts the one runs the other."""

        application = self.server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            # MCPBoundary owns Host and Origin enforcement before requests reach
            # the SDK (security.py says why its own settings are not used).
            # Keeping the SDK's localhost-only defaults would reject the
            # explicit allowlist with 421 before tool dispatch.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        return Served(application, self.server.session_manager)


@dataclass(frozen=True, slots=True)
class Served:
    application: Starlette
    sessions: StreamableHTTPSessionManager


mcp = ToolServer()
