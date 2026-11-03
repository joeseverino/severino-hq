"""The API document writes each described fragment once, and each named
alias it defines says what it is."""

import json
import sys
from dataclasses import dataclass
from typing import Annotated, TypeAliasType

from django.test import SimpleTestCase
from pydantic import Field, WithJsonSchema

from hq.platform.application.command_fields import Describe
from hq.platform.application.integration_specs import CapabilitySpec, ResourceSpec
from hq.platform.application.integrations import compile_integration_graph, override_integration_graph
from hq.platform.application.json_schemas import json_schemas
from hq.platform.application.resources import TopQuery
from hq_sdk.capabilities import StrictCommand

from .. import openapi
from ..fragments import OpenAPIError, described_once

type ExampleNotes = Annotated[str, Describe("Anything else worth keeping.")]


@dataclass(frozen=True)
class ExampleCreate:
    title: str
    notes: ExampleNotes = ""


@dataclass(frozen=True)
class ExampleRename:
    notes: ExampleNotes


# A schema written whole inside an alias replaces what a Field beside it says.
type ExampleUnsaid = Annotated[str, WithJsonSchema({"type": "string"}), Field(description="Said, then dropped.")]


@dataclass(frozen=True)
class ExampleLabel:
    label: ExampleUnsaid


class ExampleCommand(StrictCommand):
    value: int = 0


class ExampleNoteCommand(StrictCommand):
    note: str = ""


def _example_write(command: ExampleCommand, *, principal) -> dict:
    return {"ok": True}


def _example_targeted(command: StrictCommand, *, principal, current_key: str) -> dict:
    return {"ok": True}


_NOTES = {"description": "Anything else worth keeping.", "type": "string"}


def _aliases() -> set[str]:
    """The name of every named type alias HQ declares, among the modules loaded."""

    return {
        value.__name__
        for name, module in tuple(sys.modules.items())
        if name.partition(".")[0] in ("hq", "hq_sdk")
        for value in tuple(vars(module).values())
        if isinstance(value, TypeAliasType)
    }


def _undescribed(schemas: dict, aliases: set[str]) -> list[str]:
    """Each alias defined in ``schemas`` whose definition says nothing of what it is."""

    return sorted(name for name, schema in schemas.items() if name in aliases and "description" not in schema)


def _document(*schemas: dict) -> dict:
    return {"paths": {}, "components": {"schemas": {f"Example{index}": schema for index, schema in enumerate(schemas)}}}


class DescribedOnceTests(SimpleTestCase):
    def test_the_document_holds_no_two_identical_described_fragments(self):
        described_once(openapi.document())

    def test_a_document_with_no_resources_holds_no_two_identical_described_fragments(self):
        # The fuzz test's graph: one capability, and every generic list and
        # detail parameter written with no resource to name.
        spec = CapabilitySpec(
            "example.properties",
            "Exercise a generated write.",
            "remote_write",
            "example.write",
            ExampleCommand,
            _example_write,
        )
        with override_integration_graph(compile_integration_graph(capabilities=(spec,), resources=(), connections=())):
            described_once(openapi.document())

    def test_a_target_two_capabilities_take_in_the_same_words_is_written_once(self):
        # Two commands of their own, so only the target is shared.
        specs = tuple(
            CapabilitySpec(
                f"example.vehicle.{verb}",
                f"Example {verb}.",
                "remote_write",
                "example.write",
                command,
                _example_targeted,
                "key",
                target_help="The vehicle's key",
            )
            for verb, command in (("recall", ExampleCommand), ("refresh", ExampleNoteCommand))
        )
        with override_integration_graph(compile_integration_graph(capabilities=specs, resources=(), connections=())):
            document = openapi.document()

        described_once(document)
        schemas = document["components"]["schemas"]
        targets = {
            json.dumps(schemas[f"ExampleVehicle{verb}Request"]["properties"]["target"])
            for verb in ("Recall", "Refresh")
        }
        self.assertEqual(len(targets), 1)
        self.assertIn("$ref", json.loads(targets.pop()))

    def test_lists_inheriting_one_query_write_its_shared_parameters_once(self):
        # Two lists the way extensions declare them, each with a query that
        # inherits the ranked list's limit.
        class ExampleNoteQuery(TopQuery):
            pass

        class ExampleTaskQuery(TopQuery):
            pass

        def rows(*, limit: int = 50) -> dict:
            return {"items": [], "count": 0}

        specs = tuple(
            ResourceSpec(f"example.{kind}", kind.title(), f"Example {kind}.", "example.read", rows, query)
            for kind, query in (("notes", ExampleNoteQuery), ("tasks", ExampleTaskQuery))
        )
        composed = compile_integration_graph(capabilities=(), resources=specs, connections=())
        with override_integration_graph(composed):
            document = openapi.document()

        self.assertIn("/api/v2/resources/example.tasks/", document["paths"])
        described_once(document)

    def test_every_named_alias_the_document_defines_says_what_it_is(self):
        schemas = openapi.document()["components"]["schemas"]
        aliases = _aliases()

        self.assertIn("ExpectedUpdatedAt", set(schemas) & aliases)
        self.assertEqual(_undescribed(schemas, aliases), [])

    def test_an_alias_whose_whole_schema_drops_its_description_is_found(self):
        _, definitions = json_schemas(
            {"label": ExampleLabel}, mode="validation", ref_template=openapi.SCHEMAS + "{model}"
        )

        self.assertEqual(_undescribed(definitions, {"ExampleUnsaid"}), ["ExampleUnsaid"])

    def test_a_named_alias_is_one_component_and_each_use_a_reference(self):
        _, definitions = json_schemas(
            {"create": ExampleCreate, "rename": ExampleRename},
            mode="validation",
            ref_template=openapi.SCHEMAS + "{model}",
        )
        document = _document(*definitions.values())

        described_once(document)
        self.assertEqual(definitions["ExampleNotes"], _NOTES)
        self.assertEqual(
            definitions["ExampleCreate"]["properties"]["notes"],
            {"$ref": openapi.SCHEMAS + "ExampleNotes", "default": ""},
        )
        self.assertEqual(
            definitions["ExampleRename"]["properties"]["notes"], {"$ref": openapi.SCHEMAS + "ExampleNotes"}
        )

    def test_a_document_writing_a_described_fragment_twice_is_refused_and_says_to_declare_an_alias(self):
        document = _document(
            {"type": "object", "properties": {"notes": _NOTES}},
            {"type": "object", "properties": {"notes": _NOTES}},
        )

        with self.assertRaisesRegex(OpenAPIError, "named type alias"):
            described_once(document)

    def test_a_repeated_parameter_is_refused(self):
        document = {
            "paths": {},
            "components": {"parameters": {"A": {"name": "a", **_NOTES}, "B": {"name": "a", **_NOTES}}},
        }

        with self.assertRaises(OpenAPIError):
            described_once(document)

    def test_a_property_named_like_a_keyword_is_still_read(self):
        document = _document(
            {"properties": {"default": _NOTES}},
            {"properties": {"default": _NOTES}},
        )

        with self.assertRaises(OpenAPIError):
            described_once(document)

    def test_a_use_describing_itself_beside_a_described_component_is_refused(self):
        uses = {
            "a property": {"properties": {"notes": {"$ref": openapi.SCHEMAS + "Notes", "description": "Notes."}}},
            "a parameter": {
                "name": "notes",
                "in": "query",
                "description": "Notes.",
                "schema": {"$ref": openapi.SCHEMAS + "Notes"},
            },
        }
        for case, use in uses.items():
            with self.subTest(case):
                document = {"paths": {}, "components": {"schemas": {"Notes": _NOTES, "Example": use}}}

                with self.assertRaisesRegex(OpenAPIError, "bare reference"):
                    described_once(document)

    def test_a_bare_reference_to_a_described_component_and_a_described_use_of_an_undescribed_one_pass(self):
        document = {
            "paths": {},
            "components": {
                "schemas": {
                    "Notes": _NOTES,
                    "Plain": {"type": "string"},
                    "Example": {
                        "properties": {
                            "notes": {"$ref": openapi.SCHEMAS + "Notes"},
                            "plain": {"$ref": openapi.SCHEMAS + "Plain", "description": "What this use means."},
                        }
                    },
                }
            },
        }

        described_once(document)
