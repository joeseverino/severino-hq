from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp import Client

from hq.platform.api.openapi import document
from hq.platform.application.json_schemas import json_schema
from hq.platform.application.resources import resource_registry

from ..binding import bind_tools
from ..contract import served_resources
from ..declarations import HANDLERS, argument_model, tool_contract
from ..identity import set_principal
from ..listing import widest_caller
from ..server import mcp
from .registry import registry_patched


def _listed():
    async def listed():
        with set_principal(widest_caller()):
            async with Client(mcp.server) as client:
                return await client.list_tools()

    return {tool.name: tool for tool in async_to_sync(listed)().tools}


def _documented(value):
    """The resources the document's paths serve: (listed, read one at a time, by name and identifier)."""

    listed, detailed = set(), set()
    for item in value["paths"].values():
        operation = item.get("get", {})
        if name := operation.get("x-hq-resource"):
            if "x-hq-identifier" in operation:
                detailed.add((name, operation["x-hq-identifier"]))
            else:
                listed.add(name)
    return listed, detailed


class DocumentToolTests(SimpleTestCase):
    def test_every_tool_listed_is_its_handler_as_its_argument_model_publishes_it(self):
        listed = _listed()
        entries = tool_contract()
        resources = served_resources()
        self.assertEqual(set(listed), {fn.__name__ for fn in HANDLERS})
        for handler in HANDLERS:
            tool = listed[handler.__name__]
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.input_schema, json_schema(argument_model(handler, resources)))
                self.assertEqual(tool.description, entries[tool.name]["description"])
                self.assertFalse(tool.input_schema["additionalProperties"])

    def test_the_tools_are_built_without_building_the_document(self):
        with patch("hq.platform.api.openapi.document", side_effect=AssertionError("document built")):
            tool_contract()
            bind_tools()

    def test_the_resource_tools_name_what_the_document_serves(self):
        resources = served_resources()
        listed, detailed = _documented(document())
        self.assertEqual({resource.name for resource in resources.listable}, listed)
        self.assertEqual({(resource.name, resource.identifier) for resource in resources.addressable}, detailed)

    def test_a_resource_tool_takes_only_the_resources_the_registry_serves(self):
        served = {name: spec for name, spec in resource_registry().items() if name != "projects"}
        execute = AsyncMock(return_value={"items": [], "count": 0})
        with patch("hq.platform.mcp.binding.sync_to_async", return_value=execute), registry_patched(served):
            tool = bind_tools()["list_resource"]
        for arguments in ({"name": "projects"}, {"name": "assets", "filters": "{}"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                async_to_sync(tool.call)(arguments)
        execute.assert_not_awaited()
        async_to_sync(tool.call)({"name": "assets"})
        execute.assert_awaited_once_with(name="assets", filters=None, fields=None)

    def test_a_tool_with_no_description_is_refused(self):
        def example_undescribed():
            return {}

        with patch("hq.platform.mcp.declarations.HANDLERS", (example_undescribed,)), self.assertRaises(ValueError):
            tool_contract()

    def test_unknown_and_invalid_arguments_never_invoke_execution(self):
        execute = AsyncMock()
        with patch("hq.platform.mcp.binding.sync_to_async", return_value=execute):
            tool = bind_tools()["recent_activity"]
        for arguments in ({"limit": "2"}, {"limit": True}, {"limit": None}, {"unknown": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                async_to_sync(tool.call)(arguments)
        execute.assert_not_awaited()

    def test_rejected_input_values_do_not_enter_tool_errors(self):
        tools = bind_tools()
        for name, arguments in (
            ("recent_activity", {"limit": "synthetic-sensitive-input"}),
            ("list_resource", {"name": "synthetic-sensitive-input"}),
            ("execute_capability", {"name": "example.write", "payload": "synthetic-sensitive-input"}),
        ):
            with self.subTest(tool=name), self.assertRaises(ValueError) as error:
                async_to_sync(tools[name].call)(arguments)
            self.assertNotIn("synthetic-sensitive-input", str(error.exception))

    def test_every_tool_says_whether_it_can_change_anything(self):
        tools = bind_tools()

        changes = {name for name, tool in tools.items() if not tool.annotations["readOnlyHint"]}
        self.assertEqual(changes, {"execute_capability", "export_year_summary"})
        # An export records itself in the audit log and touches nothing else.
        self.assertFalse(tools["export_year_summary"].annotations["destructiveHint"])
        for name, tool in tools.items():
            with self.subTest(tool=name):
                # A read stays inside what HQ holds; running a capability may not.
                self.assertEqual(tool.annotations.get("openWorldHint", True), name == "execute_capability")
        # A capability may destroy what it acts on, and every change carries a
        # retry key, so repeating one call acts once.
        self.assertTrue(tools["execute_capability"].annotations.get("destructiveHint", True))
        self.assertTrue(tools["execute_capability"].annotations.get("idempotentHint", False))

    def test_a_call_is_validated_by_the_model_its_schema_was_published_from(self):
        resources = served_resources()
        tools = bind_tools()
        for handler in HANDLERS:
            with self.subTest(tool=handler.__name__):
                self.assertIs(tools[handler.__name__].arguments, argument_model(handler, resources))

    def test_a_resource_tool_points_to_the_catalog_rather_than_carrying_it(self):
        value = document()
        entries = tool_contract()
        for name in ("list_resource", "get_resource"):
            with self.subTest(tool=name):
                self.assertIn("describe_resources", entries[name]["description"])
                for item in value["paths"].values():
                    if "x-hq-resource" in item.get("get", {}):
                        self.assertNotIn(item["get"]["description"], entries[name]["description"])
        # What a first get_resource call needs: the field each kind is addressed by.
        for resource in served_resources().addressable:
            self.assertIn(f"`{resource.name}` by `{resource.identifier}`", entries["get_resource"]["description"])
