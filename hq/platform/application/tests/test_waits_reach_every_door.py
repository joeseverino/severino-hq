"""A refusal that says "later" says how long in a field every door carries, never only in its sentence.

The envelope's ``error.retry_after`` is the same on the machine API and /mcp/;
the API repeats it as Retry-After, and MCP repeats it in a tool result's
``_meta`` and in a protocol error's ``data``, where a client reads it without
parsing the answer.
"""

from unittest import mock

from django.test import TestCase, override_settings

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving
from hq.platform.mcp.tests.test_mcp import admitting

from .. import agent_limits
from ..security import Capability, Principal
from .test_agent_limits_at_the_doors import TOOL, _bearer, _call, _session

AT_THE_DOOR = {
    "OIDC_ISSUER": ISSUER,
    "SEVERINO_API_RESOURCE": RESOURCE,
    "SEVERINO_API_LEEWAY_SECONDS": 30,
    "OIDC_RP_SIGN_ALGO": "RS256",
}


def _prompt(ident):
    return {"jsonrpc": "2.0", "id": ident, "method": "prompts/get", "params": {"name": "needs-me"}}


@override_settings(**AT_THE_DOOR, SEVERINO_AGENT_CALLS_PER_MINUTE=1, SEVERINO_AGENT_CALL_BURST=1)
class RateWaitTests(TestCase):
    def test_the_machine_api_body_carries_the_wait_its_header_says(self):
        bearer = _bearer("example-wait-api")
        with _serving():
            self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {bearer}")
            refused = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {bearer}")

        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.json()["error"]["retry_after"], int(refused["Retry-After"]))
        self.assertGreaterEqual(refused.json()["error"]["retry_after"], 1)

    def test_a_tool_call_carries_the_wait_in_its_answer_and_its_meta(self):
        from hq.config import asgi

        bearer = _bearer("example-wait-mcp")

        async def steps(post):
            await post(bearer, _call(1))
            return await post(bearer, _call(2))

        with _serving(), mock.patch(TOOL, return_value={"connections": []}):
            throttled = _session(steps, asgi.admit_agent).json()["result"]

        wait = throttled["structuredContent"]["error"]["retry_after"]
        self.assertGreaterEqual(wait, 1)
        self.assertEqual(throttled["_meta"]["retry_after"], wait)

    def test_a_read_outside_a_tool_call_carries_the_wait_in_its_error_data(self):
        from hq.config import asgi

        bearer = _bearer("example-wait-prompt")

        async def steps(post):
            await post(bearer, _call(1))
            return await post(bearer, _prompt(2))

        with _serving(), mock.patch(TOOL, return_value={"connections": []}):
            error = _session(steps, asgi.admit_agent).json()["error"]

        self.assertEqual(error["data"]["code"], "rate_limited")
        self.assertGreaterEqual(error["data"]["retry_after"], 1)


@override_settings(**AT_THE_DOOR, SEVERINO_AGENT_CALLS_AT_ONCE=1)
class AtOnceWaitTests(TestCase):
    def test_both_doors_carry_the_wait_for_a_call_past_the_agents_calls_at_once(self):
        agent = Principal("example-wait-busy", "mcp", frozenset({Capability.READ}))

        async def steps(post):
            with agent_limits.LIMITS.occupy(agent.actor):
                return await post("token", _call(1))

        with mock.patch(TOOL):
            answer = _session(steps, admitting(lambda _: agent)).json()["result"]
        with _serving(), agent_limits.LIMITS.occupy("example-wait-busy-api"):
            api = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_bearer('example-wait-busy-api')}")

        self.assertEqual(answer["structuredContent"]["error"]["code"], "too_many_calls")
        self.assertGreaterEqual(answer["structuredContent"]["error"]["retry_after"], 1)
        self.assertEqual(answer["_meta"]["retry_after"], answer["structuredContent"]["error"]["retry_after"])
        self.assertEqual(api.json()["error"]["retry_after"], int(api["Retry-After"]))
