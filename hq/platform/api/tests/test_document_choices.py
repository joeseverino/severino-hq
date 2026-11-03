"""A value of a fixed set reads, in the published document, as an ``enum``
with each value's label beside it: the form every validator and viewer reads
as a set of values."""

from collections.abc import Iterator
from typing import Any

from django.test import SimpleTestCase

from .. import openapi


def _objects(value: Any, at: str = "#") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield at, value
        for key, inner in value.items():
            yield from _objects(inner, f"{at}/{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _objects(inner, f"{at}/{index}")


class DocumentChoicesTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()

    def test_a_fixed_set_is_an_enum_never_a_list_of_constants(self):
        listed = [
            at
            for at, schema in _objects(self.document)
            if isinstance(schema.get("oneOf"), list)
            and schema["oneOf"]
            and all(isinstance(branch, dict) and "const" in branch for branch in schema["oneOf"])
        ]

        self.assertEqual(listed, [])

    def test_a_labeled_enum_labels_every_value_it_allows(self):
        labeled = [(at, schema) for at, schema in _objects(self.document) if "x-enumDescriptions" in schema]

        self.assertTrue(labeled)
        for at, schema in labeled:
            with self.subTest(at=at):
                self.assertEqual(sorted(schema["x-enumDescriptions"]), sorted(str(value) for value in schema["enum"]))
