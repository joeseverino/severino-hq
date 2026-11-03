"""dev:look's --operation opens one operation of the API reference alone: its
tag shown, and its body and every response opened, even one whose schema
another operation draws first; one the document does not hold is refused
with those it does."""

from django.test import SimpleTestCase

from hq.platform.core.tests.browser.reference import opened_operation, opened_walk

_RECORD = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Example"}}}}
_PROBLEM = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Problem"}}}}
_DOCUMENT = {
    "tags": [
        {"name": "example", "summary": "Example"},
        {"name": "example.things", "summary": "Things", "parent": "example"},
    ],
    "paths": {
        "/api/v2/example/things/": {
            "get": {"tags": ["example.things"], "responses": {"200": _RECORD, "404": _PROBLEM}},
        },
        "/api/v2/example/things/{identifier}/": {
            "get": {"tags": ["example.things"], "responses": {"200": _RECORD, "404": _PROBLEM}},
            "post": {"tags": ["example.things"], "requestBody": _RECORD, "responses": {"200": _RECORD}},
        },
    },
}


class OperationOpenedTests(SimpleTestCase):
    def test_an_operation_opens_its_tag_and_every_part_another_operation_drew_first(self):
        asked = "GET /api/v2/example/things/{identifier}/"
        opened = opened_operation(_DOCUMENT, asked)

        self.assertEqual(opened.tags, ("example", "examplethings"))
        expected = {f"/GET/api/v2/example/things/{{identifier}}/.responses.{status}" for status in ("200", "404")}
        self.assertEqual(set(opened.parts), expected)
        # The walk over every tag opens each schema once, where it is first met.
        [walked] = opened_walk(_DOCUMENT)
        self.assertFalse(expected & set(walked.parts))

    def test_an_operation_with_a_body_opens_it_too(self):
        opened = opened_operation(_DOCUMENT, "POST /api/v2/example/things/{identifier}/")

        self.assertIn("/POST/api/v2/example/things/{identifier}/.body", opened.parts)

    def test_an_operation_the_document_does_not_hold_is_refused_with_those_it_does(self):
        with self.assertRaises(LookupError) as refused:
            opened_operation(_DOCUMENT, "GET /api/v2/example/thing/{identifier}/")

        self.assertIn("GET /api/v2/example/things/{identifier}/", str(refused.exception))
