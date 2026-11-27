"""A read is refused at the door that serves it, by the access that door declares.

Each MCP tool, view and prompt declares who may use it, and the door refuses
everyone else before the read runs, so the read behind it asks nobody again.
Every declared entry is held here, so one added later is held as it is
declared.
"""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.security import AuthorizationError, Principal
from hq.platform.core.testing import MCP_READER

from .. import prompts, resources
from ..identity import set_principal
from ..server import mcp
from ..shapes import example_call

WRITER = Principal("example-writer", "mcp", frozenset({"write_projects"}))


class ReadsRefusedAtTheDoorTests(TestCase):
    def _called(self, name, arguments, principal):
        async def run():
            async with Client(mcp.server) as client:
                return await client.call_tool(name, arguments)

        with set_principal(principal):
            return async_to_sync(run)()

    def test_every_tool_refuses_a_caller_its_declaration_does_not_admit(self):
        refused = [name for name, tool in mcp.tools.items() if not tool.requires.admits(WRITER)]
        # The reads that once refused in their own bodies are among them.
        self.assertLessEqual({"get_topology", "get_findings", "system_health", "audit_registry"}, set(refused))
        for name in refused:
            answer = self._called(name, example_call(mcp.tools[name].input_schema), WRITER)
            with self.subTest(tool=name):
                self.assertTrue(answer.is_error, answer.structured_content)
                self.assertEqual(answer.structured_content["error"]["code"], "forbidden", answer.structured_content)

    def test_a_tool_that_reads_under_read_answers_a_reader(self):
        for name in ("get_topology", "system_health", "audit_registry"):
            answer = self._called(name, example_call(mcp.tools[name].input_schema), MCP_READER)
            with self.subTest(tool=name):
                self.assertFalse(answer.is_error, answer.structured_content)

    def test_every_view_and_prompt_refuses_a_caller_its_declaration_does_not_admit(self):
        for view in resources.VIEWS.values():
            if view.requires.admits(WRITER):
                continue
            with self.subTest(view=view.name), self.assertRaises(AuthorizationError):
                resources.read(view.uri, WRITER)
        for prompt in prompts.PROMPTS.values():
            if prompt.requires.admits(WRITER):
                continue
            with self.subTest(prompt=prompt.name), self.assertRaises(AuthorizationError):
                prompts.message(prompt.name, WRITER)
