from copy import deepcopy
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from hq.platform.api.openapi import document
from .binding import register_tools
from .declarations import HANDLERS, argument_metadata


class DocumentToolTests(SimpleTestCase):
    def test_every_tool_advertises_the_document_metadata(self):
        value = document()
        target = FastMCP("contract-test")
        register_tools(target, value)
        self.assertEqual(len(target._tool_manager.list_tools()), 15)
        for tool in target._tool_manager.list_tools():
            entry = value["x-hq-mcp-tools"][tool.name]
            self.assertEqual(tool.parameters, entry["inputSchema"])
            self.assertEqual(tool.description, entry["description"])
            self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(set(value["x-hq-mcp-tools"]), {fn.__name__ for fn in HANDLERS})

    def test_altered_document_owns_advertisement_and_runtime_constraints(self):
        value = deepcopy(document())
        entry = value["x-hq-mcp-tools"]["list_resource"]
        entry["description"] = "An injected catalog description."
        entry["inputSchema"]["properties"]["name"]["enum"] = ["example.injected"]
        execute = AsyncMock(return_value={"items": [], "count": 0})
        with patch("hq.platform.mcp.binding.sync_to_async", return_value=execute), patch(
            "hq.platform.application.resources.resource_registry", side_effect=AssertionError("registry walk")
        ):
            target = FastMCP("injected-test")
            register_tools(target, value)
        tool = target._tool_manager.get_tool("list_resource")
        self.assertEqual(tool.description, entry["description"])
        with self.assertRaises(ToolError):
            async_to_sync(tool.run)({"name": "projects"})
        with self.assertRaises(ToolError):
            async_to_sync(tool.run)({"name": "example.injected", "filters": "{}"})
        execute.assert_not_awaited()
        async_to_sync(tool.run)({"name": "example.injected"})
        execute.assert_awaited_once_with(name="example.injected", filters=None)

    def test_unknown_and_invalid_arguments_never_invoke_execution(self):
        value = document()
        value["x-hq-mcp-tools"]["recent_activity"]["inputSchema"]["properties"]["limit"]["minimum"] = 2
        execute = AsyncMock()
        with patch("hq.platform.mcp.binding.sync_to_async", return_value=execute):
            target = FastMCP("invalid-test")
            register_tools(target, value)
        tool = target._tool_manager.get_tool("recent_activity")
        for arguments in ({"limit": 1}, {"limit": "2"}, {"limit": True}, {"unknown": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                async_to_sync(tool.run)(arguments)
        execute.assert_not_awaited()

    def test_signature_or_handler_set_drift_fails_before_registration(self):
        for mutate in (
            lambda value: value["x-hq-mcp-tools"].pop("system_health"),
            lambda value: value["x-hq-mcp-tools"]["recent_activity"]["inputSchema"]["properties"]["limit"].update(type="string"),
            lambda value: value["x-hq-mcp-tools"]["recent_activity"]["inputSchema"].update(required=["limit"]),
        ):
            value = document()
            mutate(value)
            target = FastMCP("drift-test")
            with self.assertRaises(ValueError):
                register_tools(target, value)
            self.assertEqual(target._tool_manager.list_tools(), [])

    def test_argument_metadata_is_derived_from_each_execution_signature(self):
        value = document()
        for handler in HANDLERS:
            actual = value["x-hq-mcp-tools"][handler.__name__]["inputSchema"]
            expected = argument_metadata(handler).arg_model.model_json_schema(by_alias=True)
            self.assertEqual(set(actual["properties"]), set(expected["properties"]))
            self.assertEqual(actual.get("required"), expected.get("required"))

    def test_rejected_input_values_do_not_enter_tool_errors(self):
        value = document()
        target = FastMCP("redaction-test")
        register_tools(target, value)
        for name, arguments in (
            ("recent_activity", {"limit": "synthetic-sensitive-input"}),
            ("list_resource", {"name": "synthetic-sensitive-input"}),
            ("execute_capability", {"name": "example.write", "payload": "synthetic-sensitive-input"}),
        ):
            with self.subTest(tool=name), self.assertRaises(ToolError) as error:
                async_to_sync(target._tool_manager.get_tool(name).run)(arguments)
            self.assertNotIn("synthetic-sensitive-input", str(error.exception))
