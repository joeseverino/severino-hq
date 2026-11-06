"""MCP's catalogs say, for the caller, whether each entry is permitted, as the API's do."""

from asgiref.sync import async_to_sync
from django.test import TestCase

from hq.platform.application.security import Capability, Principal

from ..identity import set_principal
from ..server import mcp
from .registry import KIND, example_kind, served, with_kind

READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
HOLDER = Principal("example-holder", "mcp", frozenset({Capability.READ, "example_read"}))


def call(tool: str):
    return async_to_sync(mcp.tools.get(tool).call)({})


class CatalogsSayPermittedTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(READER))

    def test_each_entry_is_permitted_exactly_when_the_reader_holds_what_it_requires(self):
        for tool, key in (
            ("describe_capabilities", "capabilities"),
            ("describe_resources", "resources"),
            ("describe_connections", "connections"),
        ):
            with self.subTest(tool):
                entries = call(tool)[key]

                self.assertTrue(entries)
                self.assertEqual(
                    {entry["name"]: entry["permitted"] for entry in entries},
                    {entry["name"]: set(entry["required_capabilities"]) <= {"read"} for entry in entries},
                )

    def test_a_kind_a_test_serves_is_in_the_catalog_permitted_as_its_list_answers(self):
        """The catalog and the list read the one registry a test serves."""

        for principal, permitted in ((HOLDER, True), (READER, False)):
            with self.subTest(principal.actor), set_principal(principal), served(with_kind(example_kind())) as server:
                catalog = async_to_sync(server.tools["describe_resources"].call)({})
                entries = {entry["name"]: entry["permitted"] for entry in catalog["resources"]}
                self.assertEqual(entries.get(KIND), permitted)
                if permitted:
                    self.assertEqual(async_to_sync(server.tools["list_resource"].call)({"name": KIND})["items"], [])
