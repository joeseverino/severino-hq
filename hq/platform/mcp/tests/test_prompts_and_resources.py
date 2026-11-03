"""What a person reaches through an agent's client besides tools: prompts and
resources, read through the SDK's own client as a client receives them, each
answered from HQ's registries as the caller may read them."""

import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client
from mcp.shared.exceptions import MCPError

from hq.platform.application.security import Capability, Principal

from ..identity import set_principal
from ..server import mcp
from .registry import KIND, example_kind, served, with_kind

READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
NOBODY = Principal("example-nobody", "mcp", frozenset())
PAUSED = "hq.platform.application.agent_access.paused_interfaces"


def _session(principal, steps, server=mcp):
    """``steps(client)`` on an in-process client of ``server`` whose every request ``principal`` makes."""

    async def run():
        async with Client(server.server) as client:
            return await steps(client)

    with set_principal(principal):
        return async_to_sync(run)()


class PromptTests(TestCase):
    def test_each_prompt_is_one_message_of_what_hq_answers_the_caller(self):
        async def steps(client):
            listed = await client.list_prompts()
            return listed, {prompt.name: await client.get_prompt(prompt.name) for prompt in listed.prompts}

        listed, answered = _session(READER, steps)

        self.assertEqual({prompt.name for prompt in listed.prompts}, {"needs-me", "status-brief", "held-requests"})
        for name, result in answered.items():
            with self.subTest(prompt=name):
                (message,) = result.messages
                self.assertEqual(message.role, "user")
                # The ask, then HQ's own answer as JSON on the line after it.
                _ask, _, said = message.content.text.partition("\n")
                self.assertIsInstance(json.loads(said), dict)

    def test_a_caller_that_may_not_read_is_refused_in_hq_words(self):
        async def steps(client):
            refused = {}
            for name in ("needs-me", "status-brief", "held-requests"):
                try:
                    await client.get_prompt(name)
                except MCPError as error:
                    refused[name] = error.error.data
            return refused

        refused = _session(NOBODY, steps)

        self.assertEqual(
            refused, {name: {"code": "forbidden"} for name in ("needs-me", "status-brief", "held-requests")}
        )

    def test_a_paused_agent_reads_no_prompt(self):
        async def steps(client):
            with self.assertRaises(MCPError) as raised:
                await client.get_prompt("needs-me")
            return raised.exception.error.data

        with mock.patch(PAUSED, return_value=frozenset({"mcp"})):
            self.assertEqual(_session(READER, steps), {"code": "agents_paused"})


class ResourceTests(TestCase):
    def test_the_fixed_views_are_listed_and_each_reads_as_json(self):
        async def steps(client):
            listed = await client.list_resources()
            return listed, {str(item.uri): await client.read_resource(str(item.uri)) for item in listed.resources}

        listed, read = _session(READER, steps)

        self.assertEqual(
            {str(item.uri) for item in listed.resources}, {"hq://action.items", "hq://approvals", "hq://hq"}
        )
        for uri, result in read.items():
            with self.subTest(uri=uri):
                (contents,) = result.contents
                self.assertEqual(contents.mime_type, "application/json")
                self.assertIsInstance(json.loads(contents.text)["items"], list)

    def test_an_extension_kind_is_a_template_read_as_get_resource_reads_it(self):
        seen = []
        holder = Principal("example-holder", "mcp", frozenset({Capability.READ, "example_read"}))

        async def steps(client):
            templates = await client.list_resource_templates()
            read = await client.read_resource("hq://example.records/one%2Ftwo")
            return templates, read

        with served(with_kind(example_kind(seen))) as server:
            templates, read = _session(holder, steps, server)

        offered = {item.name: item.uri_template for item in templates.resource_templates}
        self.assertEqual(offered[KIND], f"hq://{KIND}/{{name}}")
        self.assertEqual(json.loads(read.contents[0].text), {"name": "one/two", "state": "ok"})
        self.assertEqual(seen, ["one/two"])

    def test_a_kind_the_caller_may_not_read_is_neither_offered_nor_read(self):
        seen = []

        async def steps(client):
            templates = await client.list_resource_templates()
            with self.assertRaises(MCPError) as raised:
                await client.read_resource("hq://example.records/one")
            return templates, raised.exception.error.data

        with served(with_kind(example_kind(seen))) as server:
            templates, refused = _session(READER, steps, server)

        self.assertNotIn(KIND, {item.name for item in templates.resource_templates})
        self.assertEqual(refused, {"code": "forbidden"})
        self.assertEqual(seen, [])

    def test_an_address_hq_does_not_hold_is_invalid(self):
        async def steps(client):
            codes = {}
            for uri in ("hq://example.missing", "hq://example.missing/one", "https://example.com/hq", "hq://hq?x=1"):
                try:
                    await client.read_resource(uri)
                except MCPError as error:
                    codes[uri] = error.error.code
            return codes

        codes = _session(READER, steps)

        self.assertEqual(
            set(codes), {"hq://example.missing", "hq://example.missing/one", "https://example.com/hq", "hq://hq?x=1"}
        )
        self.assertEqual(set(codes.values()), {-32602})
