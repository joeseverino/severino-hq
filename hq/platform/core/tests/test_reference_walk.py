"""The reference opened whole opens each schema once, where the walk first
meets it, at any depth: a property whose schema an earlier part or row
already showed is left closed there, and every other row is opened."""

from django.test import SimpleTestCase

from hq.platform.core.tests.browser.reference import every_tag, opened_operation, opened_walk


def _answer(schema):
    return {"content": {"application/json": {"schema": schema}}}


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


_DOCUMENT = {
    "tags": [{"name": "example", "summary": "Example"}, {"name": "example.later", "summary": "Later"}],
    "paths": {
        "/api/v2/example/things/": {
            "get": {
                "tags": ["example"],
                "responses": {
                    "200": _answer(
                        {
                            "type": "object",
                            "properties": {
                                "data": {"type": "array", "items": _ref("Thing")},
                                "owner": {"anyOf": [_ref("Person"), {"type": "null"}]},
                            },
                        }
                    )
                },
            },
        },
        "/api/v2/example/later/": {
            "get": {
                "tags": ["example.later"],
                "responses": {"200": _answer({"type": "object", "properties": {"data": _ref("Later")}})},
            },
        },
    },
    "components": {
        "schemas": {
            "Thing": {
                "type": "object",
                "properties": {"maker": _ref("Person"), "name": {"type": "string"}, "spec": {"type": "object"}},
            },
            "Person": {"type": "object", "properties": {"name": {"type": "string"}}},
            "Later": {"type": "object", "properties": {"thing": _ref("Thing"), "reviewer": _ref("Person")}},
        }
    },
}

_THINGS = "/GET/api/v2/example/things/.responses.200"
_LATER = "/GET/api/v2/example/later/.responses.200"


class ReferenceWalkTests(SimpleTestCase):
    def test_a_schema_shown_already_is_left_closed_at_every_later_row(self):
        first, later = opened_walk(_DOCUMENT)

        # Person is first met inside the list's items, so the owner beside it
        # is left closed; the later tag's rows that show Thing and Person too.
        self.assertEqual(first.shared, (f"{_THINGS}.owner :: ",))
        self.assertEqual(set(later.shared), {f"{_LATER}.data :: thing", f"{_LATER}.data :: reviewer"})
        self.assertEqual(set(every_tag(_DOCUMENT).shared), set(first.shared) | set(later.shared))

    def test_a_row_with_nothing_to_open_is_never_named(self):
        shared = {row for opened in opened_walk(_DOCUMENT) for row in opened.shared}

        self.assertFalse({row for row in shared if row.endswith(("name", "spec"))})

    def test_an_operation_opened_alone_opens_every_row(self):
        self.assertEqual(opened_operation(_DOCUMENT, "GET /api/v2/example/later/").shared, ())
