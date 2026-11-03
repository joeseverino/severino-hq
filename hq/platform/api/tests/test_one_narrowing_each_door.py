"""A projection read publishes one narrowing on both doors: each query
parameter the API document lists for it is an argument its MCP tool lists,
with the same type, choices, bounds, default and words."""

from django.test import TestCase

from hq.platform.mcp.server import mcp

from ..openapi import document

# Each read's path and its MCP tool.
READS = (("/api/v2/topology/", "get_topology"), ("/api/v2/findings/", "get_findings"))
NULL = {"type": "null"}


def _as_a_parameter(schema):
    """An MCP argument's schema as a query parameter takes it: a null the
    argument allows is no value a query can send, so absent is its default."""

    said = {key: value for key, value in schema.items() if key != "anyOf" and (key, value) != ("default", None)}
    branches = [branch for branch in schema.get("anyOf", ()) if branch != NULL]
    return {**branches[0], **said} if len(branches) == 1 else {**said, **({"anyOf": branches} if branches else {})}


class OneNarrowingEachDoorTests(TestCase):
    def test_each_query_parameter_is_the_tools_argument(self):
        paths = document()["paths"]
        for path, tool in READS:
            with self.subTest(path=path):
                parameters = {
                    parameter["name"]: {**parameter["schema"], "description": parameter["description"]}
                    for parameter in paths[path]["get"]["parameters"]
                }
                arguments = mcp.tools[tool].input_schema["properties"]

                self.assertTrue(parameters)
                self.assertEqual(parameters, {name: _as_a_parameter(schema) for name, schema in arguments.items()})
