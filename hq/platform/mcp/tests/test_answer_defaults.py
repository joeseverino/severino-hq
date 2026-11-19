"""A success leaves out each field that holds its default, and its tool's
schema says that default; what its type leaves untyped, and a refusal, are
answered whole. MCP and the operator's shell (``hq_call``) answer each alike."""

import io
import json
from dataclasses import asdict, dataclass
from typing import Annotated, Any, TypedDict
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase
from pydantic import BaseModel

from hq.platform.application.json_schemas import Written

from ..binding import Tool
from ..server import ToolServer, _answer
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
    def answered(self, result: Any, *, refused: bool = False) -> Any:
        """``result``, returned by a tool answering ``ExampleAnswer``, as MCP
        and the operator's shell each answer it, held to be the same."""

        tool = Tool(
            "example_tool", "An example.", {"type": "object"}, {}, _NoArguments, lambda: result, answers=ExampleAnswer
        )
        answer = _answer(result, tool)
        shell = io.StringIO()
        with (
            mock.patch("hq.platform.core.management.commands.hq_call.mcp", ToolServer({tool.name: tool})),
            mock.patch("sys.stdin", io.StringIO(json.dumps({"tool": tool.name}))),
        ):
            if refused:
                with self.assertRaises(CommandError):
                    call_command("hq_call", stdout=shell)
            else:
                call_command("hq_call", stdout=shell)

        self.assertIs(answer.is_error, refused)
        self.assertEqual(json.loads(answer.content[0].text), answer.structured_content)
        self.assertEqual(json.loads(shell.getvalue()), answer.structured_content)
        return answer.structured_content

    def test_a_field_at_its_default_is_left_out_and_a_zero_figure_is_said(self):
        (item,) = self.answered({"items": [ITEM]})["items"]

        self.assertEqual(set(item), {"label", "count", "link", "schema", "by_name"})
        self.assertEqual(item["count"], 0)
        # A part written from a dataclass is read by its type's own defaults.
        self.assertEqual(item["link"], {"url": "/example/"})

    def test_what_the_type_leaves_untyped_is_answered_as_it_is(self):
        (item,) = self.answered({"items": [ITEM]})["items"]

        self.assertEqual(item["schema"], ITEM["schema"])
        self.assertEqual(item["by_name"], {"empty": ""})

    def test_a_field_that_takes_null_keeps_its_empty_text(self):
        (item,) = self.answered({"items": [{**ITEM, "parent": ""}]})["items"]

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

        self.assertEqual(self.answered(refused, refused=True), refused)

    def test_a_held_change_is_a_success_without_its_defaults(self):
        held = {"ok": True, "status": "awaiting_approval", "approval": {"id": "example-approval"}, "items": [ITEM]}

        answer = self.answered(held)

        self.assertEqual(answer["status"], "awaiting_approval")
        self.assertEqual(set(answer["items"][0]), {"label", "count", "link", "schema", "by_name"})
