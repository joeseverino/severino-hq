"""One agent's slow call never holds up another agent's call.

Served as production serves it: an event loop with no synchronous caller
above it, so each request's synchronous work goes where the door sends it.
"""

import asyncio
import threading
from unittest import mock

from django.test import TestCase

from hq.platform.application.security import Capability, Principal

from ..identity import current_principal
from ..security import MCPBoundary, TailnetGate
from ..server import mcp
from .test_mcp import admitting

TOOL = "hq.platform.mcp.services.describe_application_connections"
AGENTS = {
    "slow": Principal("example-slow", "mcp", frozenset({Capability.READ})),
    "quick": Principal("example-quick", "mcp", frozenset({Capability.READ})),
}
# Only how long a broken door takes to show it: a working one releases the slow call at once.
PATIENCE = 20


def _call(ident):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": "describe_connections"}}


def served_door():
    """The MCP door over HQ's server, with the two agents above admitted by their bearers."""

    from starlette.applications import Starlette
    from starlette.routing import Mount

    served = mcp.served()
    gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10",))
    door = MCPBoundary(served.application, gate=gate, admit=admitting(AGENTS.__getitem__))
    return served, Starlette(routes=[Mount("/api/mcp", app=door)])


async def posting(steps):
    """Run ``steps(post)``, ``post(bearer, body)`` answering one MCP request."""

    import httpx2

    served, application = served_door()
    transport = httpx2.ASGITransport(app=application, client=("100.64.0.10", 50000))
    async with served.sessions.run(), httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:

        async def post(bearer, body):
            headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json, text/event-stream"}
            return await client.post("/api/mcp/", headers=headers, json=body)

        return await steps(post)


class CallsAtOnceTests(TestCase):
    def test_a_slow_call_holds_up_no_other_agent(self):
        reached, released = threading.Event(), threading.Event()
        released_in_time = []

        def handler(**_):
            if current_principal().actor == "example-slow":
                reached.set()
                released_in_time.append(released.wait(PATIENCE))
            return {"connections": []}

        async def steps(post):
            slow = asyncio.create_task(post("slow", _call(1)))
            await asyncio.to_thread(reached.wait, PATIENCE)
            # The quick agent's whole call: its brake, its tool and its answer.
            quick = await post("quick", _call(2))
            released.set()
            return quick, await slow

        with mock.patch(TOOL, side_effect=handler):
            quick, slow = asyncio.run(posting(steps))

        self.assertEqual(released_in_time, [True], "the quick call answered while the slow one still ran")
        self.assertFalse(quick.json()["result"]["isError"], quick.text)
        self.assertFalse(slow.json()["result"]["isError"], slow.text)
