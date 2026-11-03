"""Every envelope the document names says what its ``ok`` and ``error`` are,
each in the one place that declares it."""

from django.test import SimpleTestCase

from .. import openapi


class EnvelopeWordsTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schemas = openapi.document()["components"]["schemas"]

    def envelopes(self):
        return {
            name: schema
            for name, schema in self.schemas.items()
            if name in ("Success", "Failure") or name.endswith("Refusal")
        }

    def test_each_envelope_refers_its_ok_to_one_described_component_beside_its_value(self):
        envelopes = self.envelopes()
        refs = {schema["properties"]["ok"].get("$ref") for schema in envelopes.values()}

        self.assertGreater(len(envelopes), 2)
        self.assertEqual(len(refs), 1)
        (ref,) = refs
        self.assertTrue(self.schemas[ref.removeprefix(openapi.SCHEMAS)]["description"])
        for name, schema in envelopes.items():
            with self.subTest(name):
                self.assertEqual(schema["properties"]["ok"]["const"], name == "Success")

    def test_each_refusal_keeps_the_words_the_envelope_gives_its_error(self):
        said = self.schemas["Failure"]["properties"]["error"]["description"]
        refusals = {name: schema for name, schema in self.schemas.items() if name.endswith("Refusal")}

        self.assertTrue(refusals)
        for name, schema in refusals.items():
            with self.subTest(name):
                self.assertEqual(schema["properties"]["error"]["description"], said)
