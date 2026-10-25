from copy import deepcopy
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from hq.platform.api.openapi import document

from ..binding import bind_tools
from ..contract import resource_contract
from ..declarations import HANDLERS, argument_model


class DocumentToolTests(SimpleTestCase):
    def test_every_tool_advertises_the_document_metadata(self):
        value = document()
        tools = bind_tools(value)
        self.assertEqual(len(tools), 15)
        for tool in tools.values():
            entry = value["x-hq-mcp-tools"][tool.name]
            self.assertEqual(tool.input_schema, entry["inputSchema"])
            self.assertEqual(tool.description, entry["description"])
            self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertEqual(set(value["x-hq-mcp-tools"]), {fn.__name__ for fn in HANDLERS})

    def test_a_resource_tool_takes_only_the_resources_the_document_serves(self):
        value = deepcopy(document())
        paths = value["paths"]
        for path in [path for path, item in paths.items() if item.get("get", {}).get("x-hq-resource") == "projects"]:
            del paths[path]
        entry = value["x-hq-mcp-tools"]["list_resource"]
        entry["description"] = "An injected catalog description."
        execute = AsyncMock(return_value={"items": [], "count": 0})
        with (
            patch("hq.platform.mcp.binding.sync_to_async", return_value=execute),
            patch("hq.platform.application.resources.resource_registry", side_effect=AssertionError("registry walk")),
        ):
            tool = bind_tools(value)["list_resource"]
        self.assertEqual(tool.description, entry["description"])
        for arguments in ({"name": "projects"}, {"name": "assets", "filters": "{}"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                async_to_sync(tool.call)(arguments)
        execute.assert_not_awaited()
        async_to_sync(tool.call)({"name": "assets"})
        execute.assert_awaited_once_with(name="assets", filters=None, fields=None)

    def test_unknown_and_invalid_arguments_never_invoke_execution(self):
        execute = AsyncMock()
        with patch("hq.platform.mcp.binding.sync_to_async", return_value=execute):
            tool = bind_tools(document())["recent_activity"]
        for arguments in ({"limit": "2"}, {"limit": True}, {"limit": None}, {"unknown": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                async_to_sync(tool.call)(arguments)
        execute.assert_not_awaited()

    def test_handler_set_drift_fails_before_registration(self):
        value = document()
        value["x-hq-mcp-tools"].pop("system_health")
        with self.assertRaises(ValueError):
            bind_tools(value)

    def test_each_published_schema_is_its_argument_model(self):
        value = document()
        resources = resource_contract(value)
        for handler in HANDLERS:
            with self.subTest(tool=handler.__name__):
                self.assertEqual(
                    value["x-hq-mcp-tools"][handler.__name__]["inputSchema"],
                    argument_model(handler, resources).model_json_schema(by_alias=True),
                )

    def test_rejected_input_values_do_not_enter_tool_errors(self):
        value = document()
        tools = bind_tools(value)
        for name, arguments in (
            ("recent_activity", {"limit": "synthetic-sensitive-input"}),
            ("list_resource", {"name": "synthetic-sensitive-input"}),
            ("execute_capability", {"name": "example.write", "payload": "synthetic-sensitive-input"}),
        ):
            with self.subTest(tool=name), self.assertRaises(ValueError) as error:
                async_to_sync(tools[name].call)(arguments)
            self.assertNotIn("synthetic-sensitive-input", str(error.exception))

    def test_every_tool_says_whether_it_can_change_anything(self):
        tools = bind_tools(document())

        changes = {name for name, tool in tools.items() if not tool.annotations["readOnlyHint"]}
        self.assertEqual(changes, {"execute_capability"})
        for name, tool in tools.items():
            with self.subTest(tool=name):
                # A read stays inside what HQ holds; running a capability may not.
                self.assertEqual(tool.annotations.get("openWorldHint", True), name == "execute_capability")

    def test_a_tool_that_states_no_annotations_is_refused(self):
        value = document()
        del value["x-hq-mcp-tools"]["system_health"]["annotations"]

        with self.assertRaises(ValueError):
            bind_tools(value)
