"""How a tool's answer reaches a client: a refusal is a tool error, a held change
is not, and no exception's own text crosses the adapter."""

import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import types

from hq.platform.application.security import Capability, Principal

from ..identity import set_principal
from ..server import _answer, mcp

TOOL = "hq.platform.mcp.services.describe_application_connections"
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}))


def _called(**patched):
    params = types.CallToolRequestParams(name="describe_connections", arguments={})
    with set_principal(AGENT), mock.patch(TOOL, **patched):
        return async_to_sync(mcp._call)(None, params)


class AnswerTests(TestCase):
    def test_a_refusal_is_a_tool_error_and_a_held_change_is_not(self):
        refused = {"ok": False, "error": {"code": "forbidden", "message": "Not yours."}}
        held = {"ok": True, "status": "awaiting_approval", "approval": {"id": "example"}}

        self.assertTrue(_answer(refused).is_error)
        self.assertFalse(_answer(held).is_error)
        self.assertFalse(_answer({"items": []}).is_error)

    def test_the_text_is_the_structured_answer_compactly(self):
        answer = _answer({"ok": True, "items": [{"name": "example"}]})

        self.assertEqual(json.loads(answer.content[0].text), answer.structured_content)
        self.assertNotIn("\n", answer.content[0].text)

    def test_an_unexpected_failure_says_nothing_of_itself_and_the_log_keeps_it(self):
        detail = "/srv/example/secret-path-7f3a"

        with self.assertLogs("severino.capabilities", "WARNING") as logged:
            answer = _called(side_effect=RuntimeError(detail))

        self.assertTrue(answer.is_error)
        self.assertEqual(answer.structured_content["error"]["code"], "operation_failed")
        self.assertNotIn(detail, answer.content[0].text)
        self.assertIn(detail, "\n".join(logged.output))

    def test_a_refusal_hq_wrote_reaches_the_agent_in_its_own_words(self):
        from hq.platform.application.security import AuthorizationError

        answer = _called(side_effect=AuthorizationError("This agent may not read connections."))

        self.assertTrue(answer.is_error)
        self.assertEqual(
            answer.structured_content["error"],
            {"code": "forbidden", "message": "This agent may not read connections."},
        )

    def test_an_unknown_tool_is_a_protocol_error(self):
        from mcp.shared.exceptions import MCPError

        params = types.CallToolRequestParams(name="example_missing", arguments={})
        with set_principal(AGENT), self.assertRaises(MCPError) as raised:
            async_to_sync(mcp._call)(None, params)

        self.assertEqual(raised.exception.code, types.INVALID_PARAMS)

    def test_the_instructions_fit_what_a_client_loads_up_front(self):
        self.assertLess(len(mcp.server.instructions or ""), 4096)


class UnlistedCodeTests(TestCase):
    """A handler's refusal in a code the contract holds no status for is answered as the contract answers it."""

    def test_an_unlisted_code_reaches_the_client_as_operation_failed_in_its_tools_shape(self):
        from mcp import Client

        raw = {"ok": False, "error": {"code": "example_unlisted", "details": "/srv/example/raw-detail"}}

        async def run():
            async with Client(mcp.server) as client:
                await client.list_tools()
                result = await client.call_tool("execute_capability", {"name": "example.raw", "payload": {}})
                await client.session.validate_tool_result("execute_capability", result)
                return result

        patched = mock.patch("hq.platform.mcp.services.execute_application_capability", return_value=raw)
        # A caller that may run some capability, so the call reaches the handler.
        writer = Principal("example-writer", "mcp", frozenset({Capability.READ, "write_projects"}))
        with set_principal(writer), patched, self.assertLogs("severino.capabilities", "ERROR") as logged:
            answer = async_to_sync(run)()

        self.assertTrue(answer.is_error)
        self.assertEqual(answer.structured_content["error"]["code"], "operation_failed")
        self.assertEqual(json.loads(answer.content[0].text), answer.structured_content)
        self.assertNotIn("example_unlisted", answer.content[0].text)
        self.assertNotIn("raw-detail", answer.content[0].text)
        self.assertEqual(sum("example_unlisted" in line for line in logged.output), 1)
