"""A success leaves out each field that holds its default, and its tool's
schema says that default; what its type leaves untyped, and a refusal, are
answered whole."""

import json
from dataclasses import asdict, dataclass
from typing import Annotated, Any, TypedDict

from django.test import SimpleTestCase
from pydantic import BaseModel

from hq.platform.application.json_schemas import Written

from ..binding import Tool
from ..server import _answer
from ..shapes import answer_schema


@dataclass(frozen=True)
class ExampleLink:
    url: str
    method: str = "GET"
    reason: str = ""


class ExampleItem(TypedDict):
    label: str
    note: str
    hidden: bool
    tags: list[str]
    parent: str | None
    # A figure, whose zero is said.
    count: int
    link: Annotated[dict[str, Any], Written(ExampleLink)]
    # Untyped: a JSON Schema, a record.
    schema: dict[str, Any]
    by_name: dict[str, str]


class ExampleAnswer(TypedDict):
    items: list[ExampleItem]


class _NoArguments(BaseModel):
    pass


def _tool() -> Tool:
    return Tool("example_tool", "An example.", {"type": "object"}, {}, _NoArguments, dict, answers=ExampleAnswer)


ITEM = {
    "label": "Example",
    "note": "",
    "hidden": False,
    "tags": [],
    "parent": None,
    "count": 0,
    "link": asdict(ExampleLink("/example/")),
    "schema": {"type": "object", "additionalProperties": False, "required": [], "default": ""},
    "by_name": {"empty": ""},
}


class AnswerDefaultTests(SimpleTestCase):
    def test_a_field_at_its_default_is_left_out_and_a_zero_figure_is_said(self):
        answer = _answer({"items": [ITEM]}, _tool())

        (item,) = answer.structured_content["items"]
        self.assertEqual(set(item), {"label", "count", "link", "schema", "by_name"})
        self.assertEqual(item["count"], 0)
        # A part written from a dataclass is read by its type's own defaults.
        self.assertEqual(item["link"], {"url": "/example/"})
        self.assertEqual(json.loads(answer.content[0].text), answer.structured_content)

    def test_what_the_type_leaves_untyped_is_answered_as_it_is(self):
        (item,) = _answer({"items": [ITEM]}, _tool()).structured_content["items"]

        self.assertEqual(item["schema"], ITEM["schema"])
        self.assertEqual(item["by_name"], {"empty": ""})

    def test_a_field_that_takes_null_keeps_its_empty_text(self):
        (item,) = _answer({"items": [{**ITEM, "parent": ""}]}, _tool()).structured_content["items"]

        self.assertEqual(item["parent"], "")

    def test_the_listed_schema_states_each_default_and_requires_none_of_them(self):
        item = answer_schema(ExampleAnswer)["properties"]["items"]["items"]

        # Text is left out when empty, as every field that can be empty is; a figure is always said.
        self.assertEqual(item["required"], ["count"])
        fields = item["properties"]
        said = {name: fields[name]["default"] for name in ("note", "hidden", "tags", "parent", "link", "by_name")}
        self.assertEqual(said, {"note": "", "hidden": False, "tags": [], "parent": None, "link": {}, "by_name": {}})
        self.assertNotIn("default", fields["count"])

    def test_a_refusal_is_answered_whole(self):
        refused = {"ok": False, "error": {"code": "forbidden", "message": "Not yours."}}

        answer = _answer(refused, _tool())

        self.assertEqual(answer.structured_content, refused)
        self.assertTrue(answer.is_error)
