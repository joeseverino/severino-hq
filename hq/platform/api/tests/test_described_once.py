"""The API document does not build when it says a thing twice, or a part of it not at all."""

from django.test import SimpleTestCase

from hq.platform.application.tests.test_automation_adoption import AdoptionTestCase, catalog, procedure

from ..components import SCHEMAS
from ..fragments import OpenAPIError
from ..openapi import document

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
        for words, refused_document in refused.items():
            with self.subTest(words), self.assertRaisesRegex(OpenAPIError, words):
                said_once(refused_document)

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

    def test_a_served_document_is_never_refused_and_what_it_says_wrong_is_logged(self):
        from ..fragments import said_once

        twice = _document({"A": {"properties": {"default": _NOTES}}})
        with self.settings(SEVERINO_STRICT_PRINT=False), self.assertLogs("severino.api", "ERROR"):
            self.assertIs(said_once(twice), twice)


class OperatorDataTests(AdoptionTestCase):
    def test_an_input_several_playbooks_declare_alike_is_said_once_for_all_of_them(self):
        # ``check`` and ``look`` declare ``service`` in the same words, as two of an operator's playbooks may.
        other = procedure("other")
        other["roles"][0]["argument_spec"]["options"]["service"]["description"] = "The service it reads."
        self.adopt(self.proposed(catalog(procedure("check"), procedure("look"), other)))

        said = document()
        schemas = said["components"]["schemas"]
        alike = {schemas[name]["properties"]["service"]["$ref"] for name in ("ExampleCheckInputs", "ExampleLookInputs")}
        self.assertEqual(len(alike), 1)
        self.assertEqual(schemas[alike.pop().removeprefix(SCHEMAS)]["description"], "The service.")
        self.assertEqual(schemas["ExampleOtherInputs"]["properties"]["service"]["description"], "The service it reads.")
