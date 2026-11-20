"""MCP's catalogs say, for the caller, whether each entry is permitted, as the API's do; the capability
index states only a refusal."""

from asgiref.sync import async_to_sync
from django.test import TestCase

from hq.platform.application.capabilities import describe_capabilities
from hq.platform.application.security import Capability, Principal
from hq.platform.core.testing import MCP_READER

from ..identity import set_principal
from ..server import mcp
from .registry import KIND, example_kind, served, with_kind

HOLDER = Principal("example-holder", "mcp", frozenset({Capability.READ, "example_read"}))


def call(tool: str):
    return async_to_sync(mcp.tools.get(tool).call)({})


class CatalogsSayPermittedTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(MCP_READER))

    def test_each_entry_is_permitted_exactly_when_the_reader_holds_what_it_requires(self):
        for tool, key in (
            ("describe_capabilities", "capabilities"),
            ("describe_resources", "resources"),
            ("describe_connections", "connections"),
        ):
            with self.subTest(tool):
                entries = call(tool)[key]
                # The capability index names no permission; the catalog it indexes does.
                described = (
                    describe_capabilities(principal=MCP_READER)["capabilities"] if key == "capabilities" else entries
                )
                required = {entry["name"]: set(entry["required_capabilities"]) for entry in described}

                self.assertTrue(entries)
                # The index states a refusal; the full catalogs say `permitted`. Each leaves out false.
                field, held = ("refused", False) if key == "capabilities" else ("permitted", True)
                self.assertEqual(
                    {entry["name"]: entry.get(field, False) == held for entry in entries},
                    {entry["name"]: required[entry["name"]] <= {"read"} for entry in entries},
                )

    def test_a_kind_a_test_serves_is_in_the_catalog_permitted_as_its_list_answers(self):
        """The catalog and the list read the one registry a test serves."""

        for principal, permitted in ((HOLDER, True), (MCP_READER, False)):
            with self.subTest(principal.actor), set_principal(principal), served(with_kind(example_kind())) as server:
                catalog = async_to_sync(server.tools["describe_resources"].call)({})
                entries = {entry["name"]: entry["permitted"] for entry in catalog["resources"]}
                self.assertEqual(entries.get(KIND), permitted)
                if permitted:
                    self.assertEqual(async_to_sync(server.tools["list_resource"].call)({"name": KIND})["items"], [])
