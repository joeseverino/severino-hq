"""A component of the published document is titled only where a declaration
gives it one: a named alias of a bare choice between shapes, by its name. A
viewer offering such a choice's branches then says what it is, never the
name it is filed under."""

from django.test import SimpleTestCase

from .. import openapi

_SHAPED = ("type", "properties", "enum", "const", "$ref", "allOf")


def _bare_choice(schema):
    return any(key in schema for key in ("anyOf", "oneOf")) and not any(key in schema for key in _SHAPED)


class DocumentTitleTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.components = openapi.document()["components"]["schemas"]

    def test_a_named_choice_between_shapes_is_titled(self):
        choices = {name: schema for name, schema in self.components.items() if _bare_choice(schema)}

        self.assertTrue(choices)
        self.assertEqual([name for name, schema in choices.items() if not schema.get("title")], [])

    def test_no_other_component_gains_a_title(self):
        titled = [name for name, schema in self.components.items() if "title" in schema and not _bare_choice(schema)]

        self.assertEqual(titled, [])
