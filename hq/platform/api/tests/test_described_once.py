"""The API document does not build when it says a thing twice, or a part of it not at all."""

from django.test import SimpleTestCase

from hq.platform.application.tests.test_automation_adoption import SECOND, AdoptionTestCase, catalog, procedure

from ..components import SCHEMAS, Components
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
        held = Components()
        held.add("A", _NOTES)
        with self.settings(SEVERINO_STRICT_PRINT=False), self.assertLogs("severino.api", "ERROR") as logged:
            self.assertIs(said_once(twice), twice)
            # Two parts claiming one name: the first is kept.
            held.add("A", {**_NOTES, "description": "Something else."})
        self.assertEqual(held.schemas["A"], _NOTES)
        self.assertEqual(len(logged.records), 2)


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

    def test_two_playbook_ids_one_name_in_the_api_are_refused_naming_both(self):
        alike = procedure("unit.history")
        alike["roles"][0]["argument_spec"]["options"]["service"]["description"] = "The service it reads."
        refused = self.refused(self.adopt(self.proposed(catalog(procedure("unit_history"), alike))))
        self.assertIn("example.unit_history", refused)
        self.assertIn("example.unit.history", refused)
        document()

        # One adopted from an earlier commit holds the name too, while it stays.
        self.adopt(self.proposed(catalog(procedure("unit_history"))))
        later = self.proposed(catalog(alike, revision=SECOND))
        answer = self.adopt(later, key="example-adopt-later", playbook="example.unit.history")
        self.assertFalse(answer["ok"], answer)
        self.assertIn("example.unit_history", " ".join(answer["error"]["details"]["refused"]))
        document()
