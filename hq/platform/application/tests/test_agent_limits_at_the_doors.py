"""An agent asking too much is told "later" on both doors, each in its own protocol, and nothing it asked runs.

The machine API answers 429 with Retry-After. /mcp/ answers each tool call
with an error result over a 200, since a client drops a server whose
connection is refused; its initialize and tools/list are still served.
"""

import threading
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.core.models import AuditLog
from hq.platform.mcp import binding
from hq.platform.mcp.tests.test_mcp import admitting

from .. import agent_limits
from ..security import Capability, Principal

TOOL = "hq.platform.mcp.services.describe_application_connections"
HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "example", "version": "1"}},
}


def _call(ident):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": "describe_connections"}}


def _bearer(actor):
    return _token(actor, scope="read")


def _denials(actor):
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__actor=actor)


def _session(steps, admit):
    """Run ``steps(post)`` against a served MCP door of the test's own, admitting through ``admit``."""

    import httpx2
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from hq.platform.mcp.security import MCPBoundary, TailnetGate
    from hq.platform.mcp.server import mcp

    served = mcp.served()
    gate = TailnetGate(allowed_hosts=("testserver",), allowed_networks=("100.64.0.0/10",))
    application = Starlette(routes=[Mount("/api/mcp", app=MCPBoundary(served.application, gate=gate, admit=admit))])

    async def run():
        transport = httpx2.ASGITransport(app=application, client=("100.64.0.10", 50000))
        async with (
            served.sessions.run(),
            httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
        ):

            async def post(bearer, body):
                return await client.post(
                    "/api/mcp/", headers=HEADERS | {"Authorization": f"Bearer {bearer}"}, json=body
                )

            return await steps(post)

    return async_to_sync(run)()


def _code(answer):
    result = answer.json()["result"]
    return result["structuredContent"]["error"]["code"] if result["isError"] else None


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_CALLS_PER_MINUTE=1,
    SEVERINO_AGENT_CALL_BURST=2,
)
class RateAtTheDoorsTests(TestCase):
    def test_the_machine_api_answers_429_with_retry_after_and_records_a_flood_once(self):
        bearer = _bearer("example-rate-api")
        with _serving():
            answered = [self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {bearer}") for _ in range(3)]
            written = AuditLog.objects.count()
            flood = [self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {bearer}") for _ in range(5)]

        self.assertEqual([response.status_code for response in answered], [200, 200, 429])
        refused = answered[2]
        self.assertEqual(refused.json()["error"]["code"], "rate_limited")
        self.assertGreaterEqual(int(refused["Retry-After"]), 1)
        self.assertEqual({response.status_code for response in flood}, {429})
        self.assertEqual(AuditLog.objects.count(), written, "a flood of refusals writes nothing")
        self.assertEqual(_denials("example-rate-api").count(), 1)

    def test_mcp_answers_each_tool_call_with_the_rate_and_still_serves_the_connection(self):
        from hq.config import asgi

        bearer = _bearer("example-rate-mcp")

        async def steps(post):
            return [await post(bearer, _call(1)), await post(bearer, _call(2))], [
                await post(bearer, INITIALIZE),
                await post(bearer, _call(3)),
            ]

        with _serving(), mock.patch(TOOL, return_value={"connections": []}) as tool:
            (first, second), (initialized, throttled) = _session(steps, asgi.admit_agent)

        self.assertEqual([_code(first), _code(second)], [None, None])
        self.assertEqual(initialized.status_code, 200)
        self.assertIn("serverInfo", initialized.json()["result"])
        self.assertEqual(throttled.status_code, 200, "the rate is a tool result, never an HTTP refusal")
        self.assertEqual(_code(throttled), "rate_limited")
        self.assertEqual(tool.call_count, 2)
        self.assertEqual(_denials("example-rate-mcp").filter(metadata__reason="rate_limited").count(), 1)


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_CALLS_AT_ONCE=1,
    SEVERINO_AGENT_CALLS_AT_ONCE_OVERALL=2,
)
class AtOnceAtTheDoorsTests(TestCase):
    def _api(self, actor):
        with _serving():
            return self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_bearer(actor)}")

    def test_the_machine_api_refuses_an_agent_with_all_its_calls_in_flight(self):
        with agent_limits.LIMITS.occupy("example-busy-api"):
            busy = self._api("example-busy-api")
            other = self._api("example-other-api")

        self.assertEqual(busy.status_code, 429)
        self.assertEqual(busy.json()["error"]["code"], "too_many_calls")
        self.assertGreaterEqual(int(busy["Retry-After"]), 1)
        self.assertEqual(other.status_code, 200, "another agent has its own calls at once")
        self.assertEqual(self._api("example-busy-api").status_code, 200, "the call given back is free again")

    def test_the_machine_api_refuses_every_agent_once_all_together_are_in_flight(self):
        with agent_limits.LIMITS.occupy("example-a"), agent_limits.LIMITS.occupy("example-b"):
            busy = self._api("example-c")

        self.assertEqual((busy.status_code, busy.json()["error"]["code"]), (429, "too_many_calls"))

    def test_mcp_answers_a_call_past_the_agents_calls_at_once_and_runs_nothing(self):
        agent = Principal("example-busy-mcp", "mcp", frozenset({Capability.READ}))

        async def steps(post):
            with agent_limits.LIMITS.occupy(agent.actor):
                return await post("token", _call(1))

        with mock.patch(TOOL) as tool:
            answer = _session(steps, admitting(lambda _: agent))

        tool.assert_not_called()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(_code(answer), "too_many_calls")
        self.assertEqual(agent_limits.LIMITS.held(), 0)


@override_settings(SEVERINO_AGENT_CALL_TIMEOUT_SECONDS=0)
class TimeoutTests(TestCase):
    def test_a_call_that_runs_too_long_answers_the_agent_and_is_logged(self):
        agent = Principal("example-slow-mcp", "mcp", frozenset({Capability.READ}))
        logged = threading.Event()

        def slow(**_):
            # Held until the timeout has been answered and logged, however late it started.
            logged.wait(30)
            return {"connections": []}

        async def steps(post):
            return await post("token", _call(1))

        with (
            mock.patch(TOOL, side_effect=slow),
            mock.patch.object(binding.logger, "warning", side_effect=lambda *_, **__: logged.set()) as warned,
        ):
            answer = _session(steps, admitting(lambda _: agent))

        self.assertEqual(answer.status_code, 200)
        self.assertEqual(_code(answer), "call_timed_out")
        self.assertEqual(warned.call_args.kwargs["extra"]["event"], "mcp.call.timed_out")
        self.assertEqual(agent_limits.LIMITS.held(), 0, "the call is given back once its thread ends")
