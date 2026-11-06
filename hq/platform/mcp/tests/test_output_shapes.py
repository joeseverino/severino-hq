"""Every tool lists the shape of what it answers, and each answer meets it, a refusal as much as a success.

The spec has a server's structured result meet the tool's ``outputSchema``
with no exception for ``isError``, while the SDK's client checks only a
success: so each call here is checked by the client's own validator, whatever
it answered. Each success is held to the type its handler declares, too, on
the data set the showcase serves, so a key the type requires and an answer
lacks fails here.
"""

import json

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client
from pydantic import TypeAdapter

from hq.domains.control_plane.models import CapabilityRule
from hq.domains.projects.models import Project
from hq.platform.application.security import Capability, Principal
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..contract import served_resources
from ..identity import set_principal
from ..server import mcp
from ..shapes import answer_type

# An agent holding every capability, so each kind is read rather than refused.
READER = Principal("example-reader", "mcp", frozenset(Capability))
WRITER = Principal("example-writer", "mcp", frozenset({"write_projects", "read"}))
# An agent whose changes wait for a person, so a held answer is read too.
HELD = Principal("example-held", "mcp", frozenset({"write_projects", "read"}))
PROJECT = {"name": "Shaped", "slug": "shaped", "status": "active"}
HELD_PROJECT = {"name": "Held", "slug": "held", "status": "active", "idempotency_key": "example-held"}
ARGUMENTS = {
    "export_year_summary": ({"year": 2026}, {"year": 2026, "format": "json"}),
    # One kind, with its list's whole query schema.
    "describe_resources": ({}, {"name": "projects"}),
    "execute_capability": ({"name": "project.create", "payload": PROJECT | {"idempotency_key": "example-shaped"}},),
}


def _successes(lenses):
    """Each call that answers: a tool's defaults, every kind a resource tool names, and each lens's graph."""

    contract = served_resources()
    for name in mcp.tools:
        if name == "list_resource":
            yield from ((name, {"name": kind.name}) for kind in contract.listable)
        elif name == "get_resource":
            yield name, {"name": "projects", "identifier": "read-only"}
        else:
            yield from ((name, arguments) for arguments in ARGUMENTS.get(name, ({},)))
    yield from (("get_topology", {"lens": lens}) for lens in lenses)


def _refusals():
    """A call to each tool that is refused: an argument it does not take, and a record that is not there."""

    yield from ((name, {"example_unknown": 1}) for name in mcp.tools)
    yield "get_resource", {"name": "projects", "identifier": "example-absent"}
    yield "execute_capability", {"name": "example.missing", "payload": {}}


def _field(schema, key):
    """A field by the shape that names it, so a shape two answers share is proven by either."""

    return json.dumps(schema, sort_keys=True), key


def _branches(schema):
    """``schema`` and each schema an ``anyOf`` in it offers, read whole."""

    if not isinstance(schema, dict):
        return
    yield schema
    for branch in schema.get("anyOf") or ():
        yield from _branches(branch)


def _is(schema, kind):
    named = schema.get("type")
    return named == kind or (isinstance(named, list) and kind in named)


def _named(schema, path):
    """Each field ``schema`` names only some answers hold, at any depth and in
    any branch, and where it first sits. A required one is the validator's,
    which refuses an answer that lacks it."""

    for shape in _branches(schema):
        for key, inner in (shape.get("properties") or {}).items():
            if key not in shape.get("required", ()):
                yield _field(shape, key), f"{path}.{key}"
            yield from _named(inner, f"{path}.{key}")
        if _is(shape, "array"):
            yield from _named(shape.get("items"), f"{path}[]")


def _held(schema, value, path, held):
    """Add each field ``schema`` names that ``value`` holds, by where it sits."""

    for shape in _branches(schema):
        if isinstance(value, dict):
            for key, inner in (shape.get("properties") or {}).items():
                if key in value:
                    held.add(_field(shape, key))
                    _held(inner, value[key], f"{path}.{key}", held)
        elif _is(shape, "array") and isinstance(value, list):
            for item in value:
                _held(shape.get("items"), item, f"{path}[]", held)


def _unheld(listed, answers):
    """Where each field a listed tool's success shape names sits, when no
    successful answer among ``answers`` holds it."""

    successes = {tool.name: tool.output_schema["anyOf"][0] for tool in listed}
    named = dict(field for name, schema in successes.items() for field in _named(schema, name))
    held: set[tuple[str, str]] = set()
    for name, _arguments, result in answers:
        if not result.is_error:
            _held(successes[name], result.structured_content, name, held)
    return sorted(where for field, where in named.items() if field not in held)


class OutputShapeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The estate the showcase serves, so every list an answer holds has items whose fields are read.
        with throwaway_media(), seeds.built("showcase"):
            pass
        Project.objects.create(name="Read only", slug="read-only")
        CapabilityRule.objects.create(subject=HELD.actor, capability="project.create", rule="approve")

    def _checked(self, principal, calls):
        """Each call's result, checked against its tool's listed outputSchema by the SDK's client."""

        async def run():
            answered = []
            async with Client(mcp.server) as client:
                listed = (await client.list_tools()).tools
                for name, arguments in calls:
                    result = await client.call_tool(name, arguments)
                    # The client checks a success itself; a refusal only on asking.
                    await client.session.validate_tool_result(name, result)
                    answered.append((name, arguments, result))
            return listed, answered

        with set_principal(principal):
            return async_to_sync(run)()

    def test_every_tool_lists_an_output_shape(self):
        listed, _ = self._checked(READER, [])

        for tool in listed:
            with self.subTest(tool=tool.name):
                self.assertIsNotNone(tool.output_schema)

    def test_every_answer_meets_its_tools_shape_success_and_refusal_alike(self):
        listed, first = self._checked(READER, [("get_topology", {})])
        lenses = [lens["name"] for lens in first[0][2].structured_content["lenses"]]
        writes = [(name, arguments) for name, arguments in _successes(lenses) if name == "execute_capability"]
        reads = [(name, arguments) for name, arguments in _successes(lenses) if name != "execute_capability"]

        _, answered = self._checked(READER, [*reads, *_refusals()])
        _, written = self._checked(WRITER, writes)
        _, held = self._checked(HELD, [("execute_capability", {"name": "project.create", "payload": HELD_PROJECT})])
        self.assertEqual(held[0][2].structured_content.get("status"), "awaiting_approval")
        written += held

        succeeded = {name for name, _, result in [*answered, *written] if not result.is_error}
        refused = {name for name, _, result in answered if result.is_error}
        # Each tool's success and its refusal were both read, so neither half passes unread.
        self.assertEqual(succeeded, set(mcp.tools))
        self.assertEqual(refused, set(mcp.tools))
        # A field a shape names is one an answer holds: each answer holds those
        # its record requires, as the validator checked, and some answer holds
        # every one, so no shape names a field nothing answers.
        # A trace from a node a lens holds, so a graph's edges are read too.
        nodes = [
            node["id"]
            for name, _arguments, result in answered
            if name == "get_topology" and not result.is_error
            for node in result.structured_content.get("nodes") or ()
        ]
        _, traced = self._checked(READER, [("get_topology", {"focus": nodes[0]})])
        answered += traced
        self.maxDiff = None
        self.assertEqual(_unheld(listed, [*answered, *written]), [], "a field a shape names that no answer holds")
        # Each success is of the type its handler declares: a key the type
        # requires is one the answer holds, as Pydantic reads the type.
        declared = {name: TypeAdapter(answer_type(tool.handler)) for name, tool in mcp.tools.items()}
        for name, arguments, result in [*answered, *written]:
            if not result.is_error:
                with self.subTest(tool=name, arguments=arguments):
                    declared[name].validate_python(result.structured_content)
