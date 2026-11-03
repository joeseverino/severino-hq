"""A paused agent stays connected: it lists the tools, every call it makes is
refused in the protocol, and its next call after a resume runs on the same client.

An HTTP refusal at the door would make a client drop the server until a
person reconnects it, so the brake answers each tool call instead.
"""

from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase

from hq.platform.application.security import Capability, Principal

from ..security import MCPBoundary, TailnetGate
from ..server import mcp
from .test_mcp import admitting

PAUSED = "hq.platform.application.agent_access.paused_interfaces"
TOOL = "hq.platform.mcp.services.describe_application_connections"
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}))
HEADERS = {"Authorization": "Bearer header.payload.signature", "Accept": "application/json, text/event-stream"}


def _rpc(method, params, ident):
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}


INITIALIZE = _rpc(
    "initialize",
    {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "example-client", "version": "1"}},
    1,
)
LIST = _rpc("tools/list", {}, 2)


def _call(ident):
    return _rpc("tools/call", {"name": "describe_connections", "arguments": {}}, ident)


class PauseInProtocolTests(TestCase):
    def _session(self, steps):
        """Run ``steps(post)`` on one client against a served MCP application of this test's own."""

        import httpx2
        from starlette.applications import Starlette
        from starlette.routing import Mount

        served = mcp.served()
        gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10",))
        application = Starlette(
            routes=[Mount("/api/mcp", app=MCPBoundary(served.application, gate=gate, admit=admitting(lambda _: AGENT)))]
        )

        async def run():
            transport = httpx2.ASGITransport(app=application, client=("100.64.0.10", 50000))
            async with (
                served.sessions.run(),
                httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
            ):

                async def post(body):
                    return await client.post("/api/mcp/", headers=HEADERS, json=body)

                return await steps(post)

        return async_to_sync(run)()

    def test_a_paused_agent_lists_tools_and_each_call_names_the_pause(self):
        reached = []

        async def steps(post):
            with mock.patch(PAUSED, return_value=frozenset({"mcp", "api"})):
                answers = [await post(INITIALIZE), await post(LIST), await post(_call(3))]
            answers.append(await post(_call(4)))
            return answers

        with mock.patch(TOOL, side_effect=lambda **_: reached.append(True) or {"connections": []}):
            initialized, listed, paused, resumed = self._session(steps)

        self.assertEqual(initialized.status_code, 200, initialized.text)
        self.assertIn("serverInfo", initialized.json()["result"])
        self.assertEqual(listed.status_code, 200)
        self.assertIn("describe_connections", [tool["name"] for tool in listed.json()["result"]["tools"]])

        self.assertEqual(paused.status_code, 200, "the pause is a tool result, never an HTTP refusal")
        result = paused.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "agents_paused")

        self.assertFalse(resumed.json()["result"]["isError"], resumed.text)
        self.assertEqual(reached, [True], "the paused call ran nothing; the call after the resume ran")

    def test_a_brake_added_inside_the_refusal_answers_each_call_with_its_own_code(self):
        from hq.platform.application.agent_access import AgentRefusal

        blocked = AgentRefusal("example_blocked", "This agent is blocked.")

        async def steps(post):
            return await post(_call(1))

        with (
            mock.patch.dict("hq.platform.application.refusal_status.REFUSAL_STATUS", example_blocked=403),
            mock.patch("hq.platform.application.agent_access.refusal", return_value=blocked),
            mock.patch(TOOL) as tool,
        ):
            answer = self._session(steps)

        tool.assert_not_called()
        self.assertEqual(answer.json()["result"]["structuredContent"]["error"]["code"], "example_blocked")

    def test_a_paused_call_is_recorded_by_the_agents_name(self):
        async def steps(post):
            return await post(_call(1))

        with (
            mock.patch(PAUSED, return_value=frozenset({"mcp", "api"})),
            mock.patch("hq.platform.application.agent_admission.record_denial") as recorded,
        ):
            self._session(steps)

        recorded.assert_called_once_with(
            interface="mcp", reason="agents_paused", actor="example-agent", source="100.64.0.10"
        )

    def test_no_session_is_kept_to_lose_at_a_restart(self):
        """Stateless: a client reconnecting after a restart has no session id to present."""

        async def steps(post):
            return await post(INITIALIZE)

        answer = self._session(steps)

        self.assertEqual(answer.status_code, 200)
        self.assertNotIn("mcp-session-id", {name.lower() for name in answer.headers})
