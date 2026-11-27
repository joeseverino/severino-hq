"""A field mask that cuts inside a record's nested part answers what the
document describes: the copy of its component a mask reaches promises no
field, at every depth, and a whole answer still validates."""

from typing import Annotated, Literal, TypedDict

from django.test import SimpleTestCase
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import Field
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from hq.platform.application.command_fields import Describe

from ..components import PARTIAL, Components

_URI = "urn:example:document"


class ExampleRow(TypedDict):
    slug: Annotated[str, Describe("Its address in the example.")]
    count: int


def _validator(definitions, name):
    document = {"paths": {}, "components": {"schemas": definitions}}
    registry = Registry().with_resource(_URI, Resource(contents=document, specification=DRAFT202012))
    return Draft202012Validator({"$ref": f"{_URI}#/components/schemas/{name}"}, registry=registry)


class ExampleHealth(TypedDict):
    state: str
    label: str


class ExampleCat(TypedDict):
    kind: Literal["cat"]
    name: str


class ExampleDog(TypedDict):
    kind: Literal["dog"]
    name: str


class ExampleHealthyRecord(ExampleRow):
    health: ExampleHealth
    checks: list[ExampleHealth]
    by_name: dict[str, ExampleHealth]
    last: ExampleHealth | None
    pet: Annotated[ExampleCat | ExampleDog, Field(discriminator="kind")]


class FieldMaskTests(SimpleTestCase):
    def test_every_part_a_mask_reaches_promises_no_field_and_a_whole_answer_still_validates(self):
        components = Components()
        components.mask("ExampleHealthyRecord")
        components.typed({"ExampleHealthyRecord": ExampleHealthyRecord})
        validator = _validator(components.written(), "ExampleHealthyRecord" + PARTIAL)
        health = {"state": "healthy", "label": "Working"}

        validator.validate(
            {
                "slug": "example",
                "count": 1,
                "health": health,
                "checks": [health],
                "by_name": {"example": health},
                "last": health,
                "pet": {"kind": "cat", "name": "example"},
            }
        )
        for mask, answer in {
            "health.state": {"health": {"state": "healthy"}},
            "checks.state": {"checks": [{"state": "healthy"}]},
            "by_name.example.state": {"by_name": {"example": {"state": "healthy"}}},
            "last.state": {"last": {"state": "healthy"}},
            "pet.name": {"pet": {"name": "example"}},
        }.items():
            with self.subTest(mask):
                validator.validate(answer)
        with self.assertRaises(ValidationError):
            validator.validate({"health": {"state": 1}})
