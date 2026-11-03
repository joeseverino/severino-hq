"""A type that extends another says each field it inherits once, where the
base declares it, and stays as closed as its declaration."""

from typing import Annotated, Any, TypedDict

from django.test import SimpleTestCase
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from hq.platform.application.command_fields import Describe
from hq.platform.application.json_schemas import json_schemas

from .. import openapi
from .test_described_once import described_once

_URI = "urn:example:document"


class ExampleRow(TypedDict):
    slug: Annotated[str, Describe("Its address in the example.")]
    count: int


class ExampleRecord(ExampleRow):
    notes: str


class ExampleSameRow(ExampleRow):
    pass


class ExampleFullRecord(ExampleRecord):
    history: list[str]


def _definitions(**kinds: Any) -> dict[str, Any]:
    _, definitions = json_schemas(kinds, mode="serialization", ref_template=openapi.SCHEMAS + "{model}")
    return definitions


def _validator(definitions: dict[str, Any], name: str) -> Draft202012Validator:
    document = {"paths": {}, "components": {"schemas": definitions}}
    registry = Registry().with_resource(_URI, Resource(contents=document, specification=DRAFT202012))
    return Draft202012Validator({"$ref": f"{_URI}#/components/schemas/{name}"}, registry=registry)


class InheritedFieldTests(SimpleTestCase):
    def test_an_inherited_field_refers_to_its_base_and_is_described_once(self):
        definitions = _definitions(record=ExampleRecord)
        properties = definitions["ExampleRecord"]["properties"]

        self.assertEqual(properties["slug"], {"$ref": openapi.SCHEMAS + "ExampleRow/properties/slug"})
        self.assertEqual(properties["notes"], {"type": "string"})
        self.assertEqual(definitions["ExampleRow"]["properties"]["slug"]["description"], "Its address in the example.")
        described_once({"paths": {}, "components": {"schemas": definitions}})

    def test_a_field_inherited_through_two_bases_refers_to_where_it_is_declared(self):
        definitions = _definitions(record=ExampleFullRecord)
        properties = definitions["ExampleFullRecord"]["properties"]

        self.assertEqual(properties["slug"], {"$ref": openapi.SCHEMAS + "ExampleRow/properties/slug"})
        self.assertEqual(properties["notes"], {"$ref": openapi.SCHEMAS + "ExampleRecord/properties/notes"})
        described_once({"paths": {}, "components": {"schemas": definitions}})
        _validator(definitions, "ExampleFullRecord").validate(
            {"slug": "example", "count": 1, "notes": "", "history": []}
        )

    def test_an_extension_keeps_its_fields_required_and_refuses_any_other_key(self):
        validator = _validator(_definitions(record=ExampleRecord), "ExampleRecord")
        record = {"slug": "example", "count": 1, "notes": ""}

        validator.validate(record)
        for case, value in {
            "a key neither declares": {**record, "other": 1},
            "a base field of the wrong type": {**record, "count": "one"},
            "a base field missing": {"slug": "example", "notes": ""},
        }.items():
            with self.subTest(case), self.assertRaises(ValidationError):
                validator.validate(value)

    def test_the_base_read_alone_is_as_closed_as_before(self):
        validator = _validator(_definitions(record=ExampleRecord, row=ExampleRow), "ExampleRow")

        validator.validate({"slug": "example", "count": 1})
        with self.assertRaises(ValidationError):
            validator.validate({"slug": "example", "count": 1, "notes": ""})

    def test_a_subclass_adding_no_field_is_the_base_under_its_own_name(self):
        definitions = _definitions(same=ExampleSameRow)

        self.assertNotIn("ExampleRow", definitions)
        self.assertEqual(definitions["ExampleSameRow"]["properties"]["count"], {"type": "integer"})


class FieldMaskTests(SimpleTestCase):
    def test_a_masked_component_promises_no_field_whichever_schema_writes_it_first(self):
        for first in ("record", "row"):
            with self.subTest(first=first):
                components = openapi._Components()
                if first == "record":
                    components.typed({"ExampleRecord": ExampleRecord})
                    components.mask("ExampleRow")
                else:
                    components.typed({"ExampleRow": ExampleRow})
                    components.mask("ExampleRow")
                    components.typed({"ExampleRecord": ExampleRecord})

                self.assertNotIn("required", components.schemas["ExampleRow"])
                _validator(components.schemas, "ExampleRow").validate({"slug": "example"})
