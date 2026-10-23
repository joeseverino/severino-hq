"""A value read the way a person reads it: the words every held request,
audit entry and command result is described in."""

from datetime import date

from django.test import SimpleTestCase

from ..labels import field_label
from ..moments import when_day
from ..payload_words import value_words
from ..printable import Unprintable


class ValueWordsTests(SimpleTestCase):
    def test_each_shape_reads_as_words(self):
        self.assertEqual(value_words(True), "yes")
        self.assertEqual(value_words(["a.example", "b.example"]), "a.example, b.example")
        self.assertEqual(value_words([{"doc_id": "one"}, {"doc_id": "two"}]), "2 entries")
        self.assertEqual(value_words({"created": 3, "dns_name": "a.example"}), "created 3, DNS name a.example")
        self.assertEqual(value_words({"spec": {"a": 1}}), "1 field")
        self.assertEqual(value_words(None), "")

    def test_a_moment_reads_as_hq_says_one_and_an_object_is_refused(self):
        self.assertEqual(value_words("2026-10-03"), when_day(date(2026, 10, 3)))
        self.assertEqual(value_words(date(2026, 10, 3)), when_day(date(2026, 10, 3)))
        self.assertNotIn("T", value_words("2026-10-03T09:29:00Z"))
        for text in ("0001-01-01T00:00:00", "2026-13-45", "20261003"):
            with self.subTest(text):
                self.assertEqual(value_words(text), text)
        with self.assertRaisesMessage(Unprintable, "value_words printed a builtins.object"):
            value_words(object())

    def test_a_list_is_counted_by_what_its_schema_calls_an_item(self):
        self.assertEqual(value_words([{}, {}], {"items": {"title": "Runbook"}}), "2 runbooks")

    def test_a_field_reads_by_its_title_or_its_name_as_a_sentence(self):
        self.assertEqual(field_label("confirm_prune", "Confirm Prune"), "Confirm prune")
        self.assertEqual(field_label("dns_name"), "DNS name")
        self.assertEqual(field_label("doc_id", "Document"), "Document")
