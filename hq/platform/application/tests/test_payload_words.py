"""A value read the way a person reads it: the words every held request,
audit entry and command result is described in."""

from django.test import SimpleTestCase

from ..labels import field_label
from ..payload_words import value_words


class ValueWordsTests(SimpleTestCase):
    def test_each_shape_reads_as_words(self):
        self.assertEqual(value_words(True), "yes")
        self.assertEqual(value_words(["a.example", "b.example"]), "a.example, b.example")
        self.assertEqual(value_words([{"doc_id": "one"}, {"doc_id": "two"}]), "2 entries")
        self.assertEqual(value_words({"created": 3, "dns_name": "a.example"}), "created 3, DNS name a.example")
        self.assertEqual(value_words({"spec": {"a": 1}}), "1 field")
        self.assertEqual(value_words(None), "")

    def test_a_list_is_counted_by_what_its_schema_calls_an_item(self):
        self.assertEqual(value_words([{}, {}], {"items": {"title": "Runbook"}}), "2 runbooks")

    def test_a_field_reads_by_its_title_or_its_name_as_a_sentence(self):
        self.assertEqual(field_label("confirm_prune", "Confirm Prune"), "Confirm prune")
        self.assertEqual(field_label("dns_name"), "DNS name")
        self.assertEqual(field_label("doc_id", "Document"), "Document")
