"""A refusal a model could only retry blind names, in its details, the call or field that gets past it.

``details.next`` is a call the server serves (a tool and its arguments, or a
protocol method), and ``details.field`` the argument to send; following
either is what these hold, so a refusal cannot point at a call that is
refused in turn.
"""

from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import types
from mcp.shared.exceptions import MCPError

from hq.domains.projects.models import Project
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import EmptyQuery, resource_registry
from hq.platform.application.security import Principal, mcp_principal

from ..identity import set_principal
from ..server import mcp
from .registry import _READ, KIND, served, with_kind

WRITER = Principal("example-writer", "mcp", frozenset({"write_projects", "read"}))
PROJECT = {"name": "Named next", "slug": "named-next", "status": "active"}


def _call(name, arguments):
    params = types.CallToolRequestParams(name=name, arguments=arguments)
    return async_to_sync(mcp._call)(None, params).structured_content


def _followed(next_call):
    """The answer to the call a refusal names."""

    return _call(next_call["tool"], next_call["arguments"])


class NamedNextCallTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(mcp_principal()))

    def test_an_unknown_capability_names_the_catalog_and_the_catalog_answers(self):
        refused = _call("execute_capability", {"name": "example.missing", "payload": {}})

        self.assertEqual(refused["error"]["code"], "unknown_capability")
        catalog = _followed(refused["error"]["details"]["next"])
        self.assertIn("capabilities", catalog)

    def test_a_missing_record_names_the_field_its_kind_is_addressed_by_and_the_listing_finds_it(self):
        Project.objects.create(**PROJECT)

        refused = _call("get_resource", {"name": "projects", "identifier": "example-absent"})

        self.assertEqual(refused["error"]["code"], "not_found")
        details = refused["error"]["details"]
        listed = _followed(details["next"])
        values = [item[details["identifier"]] for item in listed["items"]]
        self.assertIn(PROJECT["slug"], values)
        found = _call("get_resource", {"name": "projects", "identifier": values[0]})
        self.assertNotEqual(found.get("ok"), False, found)

    def test_an_export_format_is_one_of_the_schemas_values_and_any_other_is_named_input(self):
        schema = mcp.tools["export_year_summary"].input_schema["properties"]["format"]

        refused = _call("export_year_summary", {"year": 2026, "format": "markdown"})

        self.assertEqual(set(schema["enum"]), {"md", "json"})
        self.assertEqual(refused["error"]["code"], "invalid_input")
        self.assertIn("format", str(refused["error"]["details"]))


class NamedFieldTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(WRITER))

    def test_a_change_without_a_retry_key_names_where_the_key_goes(self):
        refused = _call("execute_capability", {"name": "project.create", "payload": PROJECT})

        self.assertEqual(refused["error"]["code"], "idempotency_key_required")
        container, _, key = refused["error"]["details"]["field"].partition(".")
        arguments = {"name": "project.create", "payload": PROJECT}
        arguments[container] = arguments[container] | {key: "example-named-key"}
        self.assertTrue(_call("execute_capability", arguments)["ok"])

    def test_a_target_refusal_names_the_target_argument(self):
        refused = _call(
            "execute_capability",
            {"name": "project.create", "payload": PROJECT | {"idempotency_key": "example-t"}, "target": "x"},
        )

        self.assertEqual(refused["error"]["code"], "target_not_allowed")
        self.assertIn(refused["error"]["details"]["field"], mcp.tools["execute_capability"].input_schema["properties"])


class NamedRequestTests(TestCase):
    """Outside a tool call, the request that lists what does exist."""

    def setUp(self):
        self.enterContext(set_principal(mcp_principal()))

    def _refused(self, request):
        with self.assertRaises(MCPError) as raised:
            async_to_sync(request)()
        return raised.exception.error.data["next"]["method"]

    def test_an_unknown_tool_prompt_or_resource_names_the_list_that_holds_each_one(self):
        def tool():
            return mcp._call(None, types.CallToolRequestParams(name="example_missing", arguments={}))

        def prompt():
            return mcp._get_prompt(None, types.GetPromptRequestParams(name="example-missing"))

        def resource():
            return mcp._read_resource(None, types.ReadResourceRequestParams(uri="hq://example.missing"))

        served = set(mcp.server._request_handlers)
        for request in (tool, prompt, resource):
            with self.subTest(request.__name__):
                self.assertIn(self._refused(request), served)


def _record(identifier):
    return {"name": identifier, "state": "ok"}


def _listing():
    return {"items": [{"name": "example-one"}], "count": 1}


class NamedServedReadTests(TestCase):
    """A read a resource does not serve names the read it does, or the catalog when the resource is gone."""

    def setUp(self):
        self.enterContext(set_principal(mcp_principal()))

    def _on(self, server, name, arguments):
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return async_to_sync(server._call)(None, params).structured_content

    def test_a_resource_gone_since_the_tools_were_bound_names_the_catalog_and_the_catalog_answers(self):
        kind = ResourceSpec(KIND, "Example", "Records.", "read", _listing, EmptyQuery, _record, "name")
        host = resource_registry()

        with served(with_kind(kind)) as server, mock.patch(_READ, return_value=host):
            refused = self._on(server, "list_resource", {"name": KIND})
            next_call = refused["error"]["details"]["next"]
            catalog = self._on(server, next_call["tool"], next_call["arguments"])

        self.assertEqual(refused["error"]["code"], "unknown_resource")
        self.assertTrue(catalog["resources"])

    def test_a_resource_with_no_listing_names_its_record_read_and_the_record_read_answers(self):
        kind = ResourceSpec(KIND, "Example", "Records.", "read", None, EmptyQuery, _record, "name")

        with served(with_kind(kind)) as server:
            refused = self._on(server, "list_resource", {"name": KIND})
            details = refused["error"]["details"]
            arguments = details["next"]["arguments"] | {"identifier": "example-one"}
            found = self._on(server, details["next"]["tool"], arguments)

        self.assertEqual(refused["error"]["code"], "unsupported_operation")
        self.assertEqual(found, _record("example-one"))

    def test_a_resource_with_no_detail_view_names_its_listing_and_the_listing_answers(self):
        kind = ResourceSpec(KIND, "Example", "Records.", "read", _listing, EmptyQuery, None, "name")

        with served(with_kind(kind)) as server:
            refused = self._on(server, "get_resource", {"name": KIND, "identifier": "example-one"})
            next_call = refused["error"]["details"]["next"]
            listed = self._on(server, next_call["tool"], next_call["arguments"])

        self.assertEqual(refused["error"]["code"], "unsupported_operation")
        self.assertEqual(listed["items"], _listing()["items"])
