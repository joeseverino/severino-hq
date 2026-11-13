"""The API document writes each described fragment once, each named alias
it defines says what it is, and each property says what it holds.

A fragment many declarations share is declared once, where its meaning
lives, as a named type alias (``json_schemas``), and the document writes it
once under that name with each use a ``$ref``. ``described_once`` refuses a
document still holding two identical described fragments, or a use that
describes itself beside a ``$ref`` to a described component. It runs on the
host's document beside the type each MCP tool answers, since a shape the two
doors share is declared once for both, and on the composed one here, never
as a step of serving the document: a wording rule fails a gate, never a
request.
"""

import json
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, TypeAliasType

from django.test import SimpleTestCase
from pydantic import Field, WithJsonSchema

from hq.platform.application.command_fields import Describe
from hq.platform.application.integration_specs import CapabilitySpec, ResourceSpec
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import compile_integration_graph
from hq.platform.application.json_schemas import json_schemas
from hq.platform.application.resources import TopQuery
from hq.platform.mcp.declarations import HANDLERS
from hq.platform.mcp.shapes import answer_type
from hq_sdk.capabilities import StrictCommand

from .. import openapi
from ..fragments import OpenAPIError

# Values the document carries rather than says: recorded data and schema values.
_VALUES = ("examples", "example", "dataValue", "value", "const", "default", "enum")
# The objects whose keys are names rather than keywords.
_NAME_MAPS = (
    "properties",
    "patternProperties",
    "dependentSchemas",
    "$defs",
    "paths",
    "schemas",
    "parameters",
    "responses",
    "headers",
    "content",
    "securitySchemes",
)


def _described(value: Any, *, names: bool = False) -> Iterator[dict[str, Any]]:
    """Every fragment under ``value`` that carries a description."""

    # A map of names (properties, paths, components) holds no keyword, so a
    # property called ``default`` is read like any other.
    if isinstance(value, list):
        for item in value:
            yield from _described(item)
        return
    if not isinstance(value, dict):
        return
    if not names and isinstance(value.get("description"), str):
        yield value
    for key, item in value.items():
        if names or (key not in _VALUES and not key.startswith("x-")):
            yield from _described(item, names=not names and key in _NAME_MAPS)


def _referred(document: dict[str, Any], value: dict[str, Any]) -> str:
    """The component ``value`` names, directly or as its ``schema``, when that
    component describes itself; else ""."""

    schema = value.get("schema")
    ref = value.get("$ref") or (schema.get("$ref") if isinstance(schema, dict) else None)
    target = _at(document, ref)
    return ref if isinstance(target, dict) and isinstance(target.get("description"), str) else ""


def _at(document: dict[str, Any], ref: Any) -> Any:
    """What a local ``$ref`` points at in ``document``, or None."""

    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    target: Any = document
    for part in ref.removeprefix("#/").split("/"):
        target = target.get(part) if isinstance(target, dict) else None
    return target


def _says(document: dict[str, Any], schema: Any, seen: frozenset[str] = frozenset()) -> bool:
    """Whether ``schema`` says what it holds: by its own description, by what
    it refers to (a component, or the base field an inherited one names), or
    by every member of its union but null."""

    if not isinstance(schema, dict):
        return False
    if isinstance(schema.get("description"), str):
        return True
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in seen:
        return _says(document, _at(document, ref), seen | {ref})
    members = [part for key in ("anyOf", "oneOf") for part in schema.get(key, ()) if part.get("type") != "null"]
    return bool(members) and all(_says(document, part, seen) for part in members)


def unsaid(document: dict[str, Any]) -> list[str]:
    """Each property of the document's components that says nothing of what
    it holds, as ``Component.property``."""

    return [
        f"{name}.{field}"
        for name, schema in document["components"]["schemas"].items()
        for field, value in (schema.get("properties") or {}).items()
        if not _says(document, value)
    ]


# The one object written bare by design: every list's own route narrows the
# base's items to its row type through ``allOf``.
_NARROWED = frozenset({"components.schemas.ResourceCollection.properties.items.items"})
_BARE = {"type": "object"}


def _bare_objects(value: Any, path: str, *, names: bool = False) -> Iterator[str]:
    """The path of each object schema under ``value`` that names none of its fields."""

    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _bare_objects(item, f"{path}.{index}")
        return
    if not isinstance(value, dict):
        return
    if not names and value == _BARE:
        yield path
    for key, item in value.items():
        if names or (key not in _VALUES and not key.startswith("x-")):
            yield from _bare_objects(item, f"{path}.{key}" if path else key, names=not names and key in _NAME_MAPS)


def shapeless(document: dict[str, Any]) -> list[str]:
    """Each object the document writes inline with no word of its shape, by its path."""

    return [path for path in _bare_objects(document, "") if path not in _NARROWED]


def described_once(document: dict[str, Any]) -> None:
    """Refuse a document holding two identical described fragments, or a
    described use of a described component, naming the first."""

    seen = Counter(json.dumps(fragment, sort_keys=True) for fragment in _described(document))
    repeated = [key for key, count in seen.items() if count > 1]
    if repeated:
        raise OpenAPIError(
            f"{len(repeated)} described fragments are written more than once, first {repeated[0][:200]}. "
            "Declare it once as a named type alias where its meaning lives "
            "(`type Notes = Annotated[str, Describe(...)]`, hq/platform/application/json_schemas.py) "
            "and use the alias in every declaration that repeats it. A parameter or response the builder "
            "writes alike for several operations is a component it names (openapi.py, `_Builder.component`)."
        )
    retold = [ref for fragment in _described(document) if (ref := _referred(document, fragment))]
    if retold:
        raise OpenAPIError(
            f"{len(retold)} uses describe themselves beside the described component they refer to, first one "
            f"of {retold[0]}, so a reader is told its meaning twice. Say it once, in the component's description, "
            "and leave the use a bare reference; a use that means something else is a component of its own."
        )


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
    def test_the_document_and_the_mcp_answers_hold_no_two_identical_described_fragments(self):
        # A tool lists its answer without words, so its declarations are read
        # here as the document would write them, beside the document's own.
        document = openapi.document()
        _, answers = json_schemas(
            {handler.__name__: answer_type(handler) for handler in HANDLERS},
            mode="serialization",
            ref_template=openapi.SCHEMAS + "{model}",
        )
        document["components"]["schemas"] = {**answers, **document["components"]["schemas"]}

        self.assertIn("AgentFinding", answers)
        described_once(document)

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

    def test_every_property_the_document_writes_says_what_it_holds(self):
        self.assertEqual(
            unsaid(openapi.document()),
            [],
            "Say what each field holds where it is declared, `Annotated[T, Describe(...)]` "
            "(hq/platform/application/describe.py), or in the docstring of the type it refers to; a meaning "
            "several types share is a named alias (hq/platform/application/answer_fields.py).",
        )

    def test_every_object_the_document_writes_says_its_shape(self):
        self.assertEqual(
            shapeless(openapi.document()),
            [],
            "Each part of an answer HQ builds names its fields: declare a TypedDict beside the record that "
            "holds it (hq/platform/application/resource_records.py), each field with `Describe`. A part in its "
            "source's own shape uses the open alias `AsRead` (hq/platform/application/answer_fields.py).",
        )

    def test_an_inline_object_naming_no_field_is_found_where_a_described_one_and_a_value_are_not(self):
        schemas = {
            "Example": {
                "properties": {
                    "bare": {"type": "object"},
                    "listed": {"items": {"type": "object"}, "type": "array"},
                    "nullable": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                    "said": {"description": "Its source's own shape.", "type": "object"},
                    "type": {"$ref": openapi.SCHEMAS + "Said"},
                },
                "examples": [{"type": "object"}],
            }
        }

        self.assertEqual(
            shapeless({"paths": {}, "components": {"schemas": schemas}}),
            [
                "components.schemas.Example.properties.bare",
                "components.schemas.Example.properties.listed.items",
                "components.schemas.Example.properties.nullable.anyOf.0",
            ],
        )

    def test_a_property_says_what_it_holds_by_itself_its_component_or_its_base_field(self):
        schemas = {
            "Said": {"description": "Said once.", "type": "object"},
            "Unsaid": {"type": "object"},
            "Row": {"properties": {"note": _NOTES}},
            "Example": {
                "properties": {
                    "own": _NOTES,
                    "component": {"$ref": openapi.SCHEMAS + "Said"},
                    "nullable": {"anyOf": [{"$ref": openapi.SCHEMAS + "Said"}, {"type": "null"}]},
                    "inherited": {"$ref": openapi.SCHEMAS + "Row/properties/note"},
                    "bare": {"type": "string"},
                    "unsaid_component": {"$ref": openapi.SCHEMAS + "Unsaid"},
                    "half_said": {"anyOf": [{"$ref": openapi.SCHEMAS + "Said"}, {"type": "string"}]},
                }
            },
        }

        self.assertEqual(
            unsaid({"paths": {}, "components": {"schemas": schemas}}),
            ["Example.bare", "Example.unsaid_component", "Example.half_said"],
        )

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
