"""describe_capabilities answers an index until a name or a resource narrows
it, and a narrowed answer holds each schema definition once."""

import json

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.capabilities import describe_capabilities
from hq.platform.application.security import Principal, operator_capabilities

from ..identity import set_principal
from ..server import mcp

READER = Principal("example-reader", "mcp", operator_capabilities())
LOCAL = "#/$defs/"


def _refs(value, found=None):
    """Each local ``$ref`` ``value`` holds, by the definition it names."""

    found = set() if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "$ref" and isinstance(inner, str) and inner.startswith(LOCAL):
                found.add(inner.removeprefix(LOCAL))
            else:
                _refs(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _refs(inner, found)
    return found


def _defined(value, found=None):
    """Each ``$defs`` map ``value`` holds."""

    found = [] if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "$defs":
                found.append(inner)
            _defined(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _defined(inner, found)
    return found


class CapabilityAnswerTests(TestCase):
    def _answered(self, **arguments):
        async def run():
            async with Client(mcp.server) as client:
                return await client.call_tool("describe_capabilities", arguments)

        with set_principal(READER):
            answer = async_to_sync(run)()
        self.assertFalse(answer.is_error, answer.structured_content)
        return json.loads(answer.content[0].text)

    def test_with_no_narrowing_it_names_every_capability_without_a_schema(self):
        everything = describe_capabilities(principal=READER)["capabilities"]
        index = self._answered()

        self.assertEqual([entry["name"] for entry in index["capabilities"]], [entry["name"] for entry in everything])
        self.assertTrue(any(entry.get("permitted") for entry in index["capabilities"]))
        self.assertEqual(_defined(index), [])
        self.assertFalse(any("input_schema" in entry for entry in index["capabilities"]))

    def test_a_narrowed_answer_holds_each_definition_once_and_every_reference_finds_it(self):
        for arguments in ({"name": "project"}, {"resource": "infrastructure"}):
            with self.subTest(arguments=arguments):
                answer = self._answered(**arguments)
                entries = answer["capabilities"]
                definitions = answer.get("$defs", {})

                self.assertTrue(entries)
                self.assertTrue(all("input_schema" in entry for entry in entries))
                self.assertEqual(_defined(entries), [], "no schema carries definitions of its own")
                self.assertLessEqual(_refs(answer), set(definitions))

    def test_every_capabilitys_narrowed_schema_reads_as_its_whole_one_does(self):
        whole = {
            entry["name"]: entry["input_schema"] for entry in describe_capabilities(principal=READER)["capabilities"]
        }

        for segment in sorted({name.split(".")[0] for name in whole}):
            answer = self._answered(name=segment)
            definitions = answer.get("$defs", {})
            for entry in answer["capabilities"]:
                with self.subTest(capability=entry["name"]):
                    schema = whole[entry["name"]]
                    own = {name: definitions.get(name) for name in schema.get("$defs", {})}
                    self.assertEqual({**entry["input_schema"], **({"$defs": own} if own else {})}, schema)

    def test_two_commands_defining_one_name_differently_are_refused(self):
        from ..capability_answer import ClashingDefinition, shared_definitions

        entries = [
            {"name": f"example.{index}", "input_schema": {"$defs": {"Slug": {"type": kind}}}}
            for index, kind in enumerate(("string", "integer"))
        ]

        with self.assertRaisesRegex(ClashingDefinition, "Slug"):
            shared_definitions(entries)
