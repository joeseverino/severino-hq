from typing import Annotated, Any, TypedDict

from django.test import SimpleTestCase
from pydantic import BaseModel, Field

from ..json_schemas import json_schema
from ..labels import field_label


class _Command(BaseModel):
    item_name: str
    dns_name: Annotated[str, Field(title="Hostname")] = ""


class _Detail(TypedDict):
    code: str
    extra: dict[str, Any]


class GeneratorTests(SimpleTestCase):
    def test_a_title_is_written_only_where_a_declaration_gives_it(self):
        schema = json_schema(_Command)

        self.assertNotIn("title", schema)
        self.assertNotIn("title", schema["properties"]["item_name"])
        self.assertEqual(schema["properties"]["dns_name"]["title"], "Hostname")

    def test_a_field_without_a_title_is_labeled_from_its_name(self):
        properties = json_schema(_Command)["properties"]

        self.assertEqual(
            [field_label(name, schema.get("title")) for name, schema in properties.items()],
            ["Item name", "Hostname"],
        )

    def test_a_typed_dict_is_closed_and_an_open_object_says_nothing(self):
        schema = json_schema(_Detail)

        self.assertEqual(schema.get("additionalProperties", True), False)
        self.assertNotIn("additionalProperties", schema["properties"]["extra"])
