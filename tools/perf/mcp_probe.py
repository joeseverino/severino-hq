"""What HQ's MCP server costs a served process, and the tool list it answers, in a process of its own.

Run by tools.perf.mcp as ``python -m tools.perf.mcp_probe <out>``, from the tree it measures, so
the count starts before HQ imports anything. The process is the one an agent's first request
meets: imports lazy, as hq/config/asgi.py makes them, and the readiness page already served, so
what any served process holds is boot. The server then loads as that first request loads it
(asgi.py starts it on demand), and answers ``initialize`` and ``tools/list`` over its own
streamable HTTP application, as a client's POSTs reach it, then the prompt, resource and resource
template lists, and the structured answers of a describe_resources call with no arguments and of
the action queue's largest page through list_resource; a list the server does not serve counts nothing. The templates are the caller's,
so every request is asked as the agent holding the whole ceiling. Each stage's growth is counted twice:
what tracemalloc traced, charged to each module as audit:memory charges it, and the resident set,
less tracemalloc's own, which also holds what pydantic-core and other native code allocate.

A last stage of its own, so it never adds to the first request's numbers, calls a slow tool through
the agent door (``MCPBoundary``) as many times at once as the agent limits let every agent together,
each call one agent's, and the same calls one after another: what that costs the process while they
are in flight, resident (sampled from ``ps``, since the peak cannot fall between the two), traced,
and the threads and calls at the peak. The tool is the real call path with a handler that opens its
thread's database connection, as a read does, and holds until every call has arrived.

``MCP_PROBE_DEFER_BUILD=1`` sets pydantic's ``defer_build`` on the SDK's wire models, in this
process only, to measure what that upstream change would save.
"""

import asyncio
import gc
import importlib
import json
import os
import resource
import subprocess
import sys
import threading
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

lazy from django.conf import settings
lazy from django.db import connection
lazy from pydantic import BaseModel, ConfigDict

from hq.config.imports import lazily
from tools.perf.memory_probe import BOOT, READY, Ledger, walk
lazy from hq.platform.application.security import MCP_INTERFACE, Capability, Principal, mcp_principal
lazy from hq.platform.mcp.binding import Tool
lazy from hq.platform.mcp.identity import set_principal
lazy from hq.platform.mcp.security import MCPBoundary, TailnetGate
lazy from hq.platform.mcp.server import ToolServer

SERVER = "hq.platform.mcp.server"
LOADED = "server"
ANSWERED = "answer"
# The other lists, whose loads are kept out of the first request's.
LISTED = "lists"
# The slow calls, one after another and then all at once.
CALLED = "calls"
STAGES = (BOOT, LOADED, ANSWERED, LISTED, CALLED)
SERIAL = "one after another"
AT_ONCE = "at once"
SLOW = "probe_slow_call"
# How long a call may take to arrive before the probe gives up on the stage.
ARRIVAL_SECONDS = 60
# The SDK's own distributions, mcp and mcp-types, by their top-level packages.
SDK_PACKAGES = frozenset({"mcp", "mcp_types"})
# What a server's first call to it builds, measured apart when the server builds it.
DOCUMENT = ("hq.platform.api.openapi", "document")
# The base every generated wire model of the SDK's derives from.
WIRE = ("mcp_types._wire_base", "WireModel")
DEFER_BUILD = "MCP_PROBE_DEFER_BUILD"
HEADERS = [
    (b"host", b"127.0.0.1"),
    (b"content-type", b"application/json"),
    (b"accept", b"application/json, text/event-stream"),
]
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "audit-mcp", "version": "1"},
    },
}
LIST = "tools/list"
# The other lists a client asks for, measured after the first answer.
LISTS = ("prompts/list", "resources/list", "resources/templates/list")
# The catalog call an agent makes first, measured by its structured answer's bytes.
CATALOG = "describe_resources"
# The list read an agent makes next, the action queue at its largest page, measured by its
# structured answer's bytes and the items it holds.
QUEUE = "list_resource action_items"
QUEUE_LIMIT = 100


def queue_call(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """The queue's largest page, asked as the listed list_resource takes its limit:
    an argument of its own, or a filter where a ref's tool has no such argument."""

    (listing,) = (tool for tool in tools if tool["name"] == "list_resource")
    if "limit" in listing["inputSchema"].get("properties", {}):
        return {"name": "list_resource", "arguments": {"name": "action_items", "limit": QUEUE_LIMIT}}
    return {"name": "list_resource", "arguments": {"name": "action_items", "filters": {"limit": QUEUE_LIMIT}}}


QUEUE_ITEMS = f"{QUEUE} items"


def tool_calls(tools: list[dict[str, Any]]) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Each tool call the probe makes of the served list, by the name its answer is counted under."""

    return ((CATALOG, {"name": CATALOG, "arguments": {}}), (QUEUE, queue_call(tools)))


def resident() -> int:
    """The resident set's peak in bytes, less what tracemalloc holds for its traces: getrusage
    counts bytes on macOS, KiB on Linux."""

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (peak if sys.platform == "darwin" else peak * 1024) - tracemalloc.get_tracemalloc_memory()


def is_sdk(module: str) -> bool:
    return module.split(".", maxsplit=1)[0] in SDK_PACKAGES


class Probe(Ledger):
    """The ledger, and the hooks a module gets as it loads: the document's build measured, and
    the SDK's wire models deferred when asked."""

    def __init__(self, *, defer: bool) -> None:
        super().__init__()
        self.defer = defer
        self.document: dict[str, int] = {"calls": 0, "traced": 0, "peak": 0, "resident": 0}

    def _load(self, spec):
        module = super()._load(spec)
        if spec.name == DOCUMENT[0]:
            setattr(module, DOCUMENT[1], self._measured(getattr(module, DOCUMENT[1])))
        elif spec.name == WIRE[0] and self.defer:
            getattr(module, WIRE[1]).model_config["defer_build"] = True
        return module

    def _measured(self, build):
        def measured(*args, **kwargs):
            before, before_resident = tracemalloc.get_traced_memory()[0], resident()
            tracemalloc.reset_peak()
            value = build(*args, **kwargs)
            now, peak = tracemalloc.get_traced_memory()
            self.document["calls"] += 1
            self.document["traced"] += now - before
            self.document["peak"] = max(self.document["peak"], peak - before)
            self.document["resident"] += resident() - before_resident
            return value

        return measured


async def post(application, body: dict[str, Any], headers: list[tuple[bytes, bytes]] = HEADERS) -> tuple[int, bytes]:
    """The status and body ``application`` answers a JSON-RPC POST of ``body`` with."""

    sent: list[dict] = []
    requests = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]

    async def receive():
        if requests:
            return requests.pop()
        return await asyncio.get_running_loop().create_future()

    async def send(message):
        sent.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        },
        receive,
        send,
    )
    status = next(message for message in sent if message["type"] == "http.response.start")["status"]
    return status, b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")


def _asked(method: str, ident: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": {}}


async def _answer(application, body: dict[str, Any], headers: list[tuple[bytes, bytes]] = HEADERS) -> dict[str, Any]:
    status, text = await post(application, body, headers)
    if status != 200:
        raise SystemExit(f"mcp_probe: {body['method']} answered {status}: {text[:500]!r}")
    return json.loads(text)


async def answered(server, counted: Callable[[], None]) -> tuple[dict[str, Any], dict[str, int]]:
    """The tools/list result the server's own application answers, after an initialize, and,
    once ``counted`` has closed the first answer's stage, each other list's result bytes: 0 for
    one it refuses."""

    served = server.served()
    lists: dict[str, int] = {}
    async with served.sessions.run():
        await _answer(served.application, INITIALIZE)
        result = (await _answer(served.application, _asked(LIST, 2)))["result"]
        counted()
        for ident, method in enumerate(LISTS, start=3):
            answer = await _answer(served.application, _asked(method, ident))
            lists[method] = len(compact(answer["result"])) if "result" in answer else 0
        for ident, (name, params) in enumerate(tool_calls(result["tools"]), start=len(LISTS) + 3):
            answer = (await _answer(served.application, {**_asked("tools/call", ident), "params": params}))["result"]
            if answer.get("isError"):
                raise SystemExit(f"mcp_probe: {name} was refused: {answer}")
            lists[name] = len(compact(answer["structuredContent"]))
        lists[QUEUE_ITEMS] = len(answer["structuredContent"]["items"])
    return result, lists


def resident_now() -> int:
    """The resident set now, in bytes, less what tracemalloc holds for its traces: ``ps`` says it in
    KiB on macOS and Linux, and the standard library reads only the peak."""

    said = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True)
    return int(said.stdout) * 1024 - tracemalloc.get_tracemalloc_memory()


class Slow:
    """A tool's handler that opens its thread's connection and holds until released, counting
    the calls and threads at their most."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.in_flight = self.most = self.threads = 0
        self._wanted = 0
        self._arrived: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def expect(self, count: int) -> asyncio.Event:
        """An event set once ``count`` calls are in flight together."""

        self._loop, self._arrived, self._wanted = asyncio.get_running_loop(), asyncio.Event(), count
        self.release.clear()
        return self._arrived

    def __call__(self, **_: Any) -> dict[str, Any]:
        with connection.cursor() as cursor:
            cursor.execute("select 1")
        with self._lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
            self.threads = max(self.threads, threading.active_count())
            if self.in_flight == self._wanted and self._loop is not None and self._arrived is not None:
                self._loop.call_soon_threadsafe(self._arrived.set)
        held = self.release.wait(ARRIVAL_SECONDS)
        with self._lock:
            self.in_flight -= 1
        if not held:
            # A tool's failure is its answer, which call() refuses.
            raise RuntimeError(f"a slow call was never released within {ARRIVAL_SECONDS} seconds")
        return {"connections": []}


def slow_tool(slow: Slow) -> Tool:
    """The tool the slow calls run, on the real call path."""

    # Declared here, since pydantic loads only once the ledger counts.
    class NoArguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    return Tool(SLOW, "A probe's slow call.", {"type": "object"}, {}, NoArguments, slow)


async def calls(count: int) -> dict[str, dict[str, int]]:
    """What ``count`` slow calls, each one agent's, cost while in flight: one after another, then all at once."""

    slow = Slow()
    served = ToolServer({SLOW: slow_tool(slow)}).served()
    gate = TailnetGate(allowed_hosts=("127.0.0.1",), allowed_networks=("127.0.0.0/8",))
    agents = {
        f"probe-{i}": Principal(f"example-probe-{i}", MCP_INTERFACE, frozenset({Capability.READ})) for i in range(count)
    }

    async def admit(bearer: str, _source: str) -> Principal:
        return agents[bearer]

    door = MCPBoundary(served.application, gate=gate, admit=admit)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": SLOW, "arguments": {}}}

    async def call(bearer: str) -> None:
        answer = await _answer(door, body, [*HEADERS, (b"authorization", f"Bearer {bearer}".encode())])
        if answer["result"].get("isError"):
            raise SystemExit(f"mcp_probe: a slow call was refused: {answer['result']}")

    async def held(bearers: list[str], arrived: asyncio.Event) -> int:
        """The resident set while ``bearers``' calls are all in flight, after they are answered."""

        running = [asyncio.create_task(call(bearer)) for bearer in bearers]
        try:
            await asyncio.wait_for(arrived.wait(), ARRIVAL_SECONDS)
        except TimeoutError:
            slow.release.set()
            raise SystemExit(f"mcp_probe: {len(bearers)} slow calls never were in flight together") from None
        now = resident_now()
        slow.release.set()
        await asyncio.gather(*running)
        return now

    measured: dict[str, dict[str, int]] = {}
    async with served.sessions.run():
        # The first call's imports and caches, kept out of both counts.
        slow.release.set()
        await call(next(iter(agents)))
        for phase in (SERIAL, AT_ONCE):
            slow.most = slow.threads = 0
            gc.collect()
            before, traced = resident_now(), tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            if phase == SERIAL:
                highest = max([await held([bearer], slow.expect(1)) for bearer in agents])
            else:
                highest = await held(list(agents), slow.expect(count))
            measured[phase] = {
                "resident": highest - before,
                "traced": tracemalloc.get_traced_memory()[1] - traced,
                "threads": slow.threads,
                "in flight": slow.most,
            }
    return measured


def compact(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def main(out: str) -> None:
    probe = Probe(defer=os.environ.get(DEFER_BUILD) == "1")
    probe.install()
    tracemalloc.start()
    traced: dict[str, int] = {}
    held: dict[str, int] = {}
    before, before_resident = 0, resident()
    found: dict[str, Any] = {}

    def boot():
        lazily()
        served = importlib.import_module("hq.config.asgi")
        found["ready"] = asyncio.run(walk(served.application, [READY], ""))[READY]["status"]

    def load():
        found["server"] = importlib.import_module(SERVER)

    def counted():
        """The current stage's growth, and the next stage started."""

        nonlocal before, before_resident
        now, now_resident = tracemalloc.get_traced_memory()[0], resident()
        traced[probe.current], held[probe.current] = now - before, now_resident - before_resident
        before, before_resident = now, now_resident
        probe.current = STAGES[STAGES.index(probe.current) + 1]

    def answer():
        # The resource templates are the caller's: asked as the agent holding the whole ceiling.
        with set_principal(mcp_principal()):
            found["result"], found["lists"] = asyncio.run(answered(found["server"].mcp, counted))

    probe.current = BOOT
    boot()
    counted()
    load()
    counted()
    answer()
    counted()
    found["calls"] = asyncio.run(calls(settings.SEVERINO_AGENT_CALLS_AT_ONCE_OVERALL))
    probe.remove()
    tracemalloc.stop()
    result = found["result"]
    Path(out).write_text(
        json.dumps(
            {
                "ready": found["ready"],
                "deferred": probe.defer,
                "list": {"bytes": len(compact(result)), "tools": len(result["tools"])},
                "lists": found["lists"],
                "tools": {
                    tool["name"]: {part: len(compact(value)) for part, value in tool.items() if part != "name"}
                    for tool in result["tools"]
                },
                "traced": traced,
                "resident": held,
                "document": probe.document,
                "calls": found["calls"],
                "modules": {name: {"own": size, "stage": probe.stage[name]} for name, size in probe.own.items()},
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main(sys.argv[1])
