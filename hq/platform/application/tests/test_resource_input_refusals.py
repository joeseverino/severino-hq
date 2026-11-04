"""An input a resource's read refuses names, beside each field that was wrong,
the filters that kind's list takes and the call that says each one's type, so
the next call can be right. Said once, where the refusal is raised, so every
door (MCP, the API, the CLI) answers it alike."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import types

from hq.platform.application.refusal_status import refusal_of
from hq.platform.application.resources import InvalidResourceInput, list_resource, resource_registry
from hq.platform.application.security import Capability, Principal
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp

READER = Principal("example-reader", "mcp", frozenset(Capability))


def _filters(kind):
    """What ``kind``'s list narrows by: its query's fields, without the two that page it."""

    return [name for name in resource_registry()[kind].list_query_type.model_fields if name not in {"limit", "cursor"}]


def _call(name, arguments):
    params = types.CallToolRequestParams(name=name, arguments=arguments)
    with set_principal(READER):
        return async_to_sync(mcp._call)(None, params).structured_content


class ResourceInputRefusalTests(TestCase):
    def test_a_refused_filter_names_the_filters_its_kind_takes_and_where_their_types_are(self):
        with self.assertRaises(InvalidResourceInput) as raised:
            list_resource("projects", {"example_unknown": 1}, principal=READER)

        details = refusal_of(raised.exception).details
        self.assertEqual(details["filters"], _filters("projects"))
        self.assertEqual(details["next"], {"tool": "describe_resources", "arguments": {"name": "projects"}})
        self.assertEqual([error["loc"] for error in details["errors"]], [["example_unknown"]])

    def test_the_named_call_answers_that_kinds_whole_schema(self):
        refused = _call("list_resource", {"name": "projects", "filters": {"status": "example-outside"}})

        self.assertEqual(refused["error"]["code"], "invalid_input")
        following = refused["error"]["details"]["next"]
        answer = _call(following["tool"], following["arguments"])
        (entry,) = answer["resources"]
        self.assertEqual(entry["name"], "projects")
        self.assertIn("status", entry["operations"]["list"]["query_schema"]["properties"])
        self.assertEqual(refused["error"]["details"]["filters"], _filters("projects"))
