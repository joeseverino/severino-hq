"""Every example the document carries is one the API reference draws as a
sample that wraps, and one its schema accepts.

The viewer (Scalar's @scalar/api-reference, at the version
static/vendor/scalar/UPSTREAM names) draws a sample longer than 20,000
characters, printed as JSON indented by two, as a virtual block whose
lines never wrap, so a reader scrolls it sideways: `lQn=2e4` in
`ExampleResponse` and `nQn=2e4` in `ExampleSchema`
(static/vendor/scalar/standalone.js line 3433), and the same bound in
`CodeExample` for a request's sample (line 1102). The recorder cuts a
large answer's lists to one item (``testing._one_item``), and this holds
every example it leaves to the bound, measured as the viewer measures it:
``JSON.stringify(value, null, 2).length``, in UTF-16 code units.
"""

import json
import unittest
from typing import Any

from django.test import SimpleTestCase

from .. import openapi
from ..testing import RECORDING, Contract

# The viewer's bound, from the module docstring's lines.
_UNWRAPPED_PAST = 20_000

_JSON = "application/json"


def _drawn_length(value: object) -> int:
    return len(json.dumps(value, indent=2, ensure_ascii=False).encode("utf-16-le")) // 2


def _examples(document: dict[str, Any]):
    """Each example the document's parts carry: its name, the pointer to the
    schema it shows, and its value."""

    recorded = document["components"].get("examples", {})
    for path, item in document["paths"].items():
        for method, operation in item.items():
            parts = [(["requestBody"], operation["requestBody"])] if "requestBody" in operation else []
            parts += [(["responses", status], answer) for status, answer in operation["responses"].items()]
            for at, part in parts:
                for example in part.get("content", {}).get(_JSON, {}).get("examples", {}).values():
                    name = example["$ref"].rsplit("/", 1)[1] if "$ref" in example else f"{method} {path}"
                    entry = recorded[name] if "$ref" in example else example
                    pointer = ["paths", path, method, *at, "content", _JSON, "schema"]
                    yield name, pointer, entry.get("dataValue", entry.get("value"))


@unittest.skipIf(RECORDING, "the recording run rewrites the examples these read")
class ExampleSizeTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()
        cls.examples = list(_examples(cls.document))

    def test_the_document_carries_examples(self):
        self.assertGreater(len(self.examples), 10)

    def test_every_example_is_drawn_as_a_sample_that_wraps(self):
        over = {
            name: _drawn_length(value) for name, _, value in self.examples if _drawn_length(value) > _UNWRAPPED_PAST
        }

        self.assertEqual(over, {}, f"past the viewer's {_UNWRAPPED_PAST:,} characters, drawn unwrapped")

    def test_every_example_is_one_its_schema_accepts(self):
        contract = Contract(self.document)
        for name, pointer, value in self.examples:
            with self.subTest(example=name):
                contract.validate(pointer, value)
