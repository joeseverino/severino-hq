"""The API document does not build when it says a thing twice, or a part of it not at all."""

from django.test import SimpleTestCase

from ..components import SCHEMAS
from ..fragments import OpenAPIError

_NOTES = {"description": "Anything else worth keeping.", "type": "string"}
_SAID = {"Notes": _NOTES}


def _document(schemas, **parts):
    return {"paths": {}, "tags": [], "components": {"schemas": {**_SAID, **schemas}, **parts}}


class SaidOnceTests(SimpleTestCase):
    def test_a_document_saying_a_thing_twice_or_a_part_not_at_all_does_not_build(self):
        from ..fragments import said_once

        refused = {
            "named type alias": _document({"A": {"properties": {"default": _NOTES}}}),
            "bare reference": _document(
                {"A": {"properties": {"notes": {"$ref": SCHEMAS + "Notes", "description": "Notes."}}}}
            ),
            "says nothing of what it holds": _document(
                {"A": {"properties": {"half": {"anyOf": [{"$ref": SCHEMAS + "Notes"}, {"type": "string"}]}}}}
            ),
            "naming none of its fields": _document(
                {"A": {"properties": {"rows": {"items": {"type": "object"}, "type": "array", "description": "Rows."}}}}
            ),
            "parameter": _document({}, parameters={"A": {"name": "a", "in": "query", "schema": {"type": "string"}}}),
        }
        for words, document in refused.items():
            with self.subTest(words), self.assertRaisesRegex(OpenAPIError, words):
                said_once(document)

        said = _document(
            {
                "A": {
                    "properties": {
                        "notes": {"$ref": SCHEMAS + "Notes"},
                        "nullable": {"anyOf": [{"$ref": SCHEMAS + "Notes"}, {"type": "null"}]},
                        "inherited": {"$ref": SCHEMAS + "Notes"},
                    }
                }
            }
        )
        self.assertIs(said_once(said), said)
