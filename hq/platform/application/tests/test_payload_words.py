"""A value read the way a person reads it: the words every held request,
audit entry and command result is described in."""

from django.test import SimpleTestCase

from ..payload_words import value_words


class ValueWordsTests(SimpleTestCase):
    def test_each_shape_reads_as_words(self):
        self.assertEqual(value_words(True), "yes")
        self.assertEqual(value_words(["a.example", "b.example"]), "a.example, b.example")
        self.assertEqual(value_words([{"enabled": True}, {"enabled": False}]), "2 entries")
        self.assertEqual(value_words({"created": 3, "dns_name": "a.example"}), "created 3, DNS name a.example")
        self.assertEqual(value_words({"spec": {"a": 1}}), "1 field")
        self.assertEqual(value_words(None), "")

    def test_a_list_is_counted_by_what_its_schema_calls_an_item(self):
        self.assertEqual(value_words([{}, {}], {"items": {"title": "Runbook"}}), "2 runbooks")
        self.assertEqual(value_words(({},), {"anyOf": [{"type": "null"}, {"items": {"title": "Zone"}}]}), "1 zone")
        # A title of several words names the list rather than one item of it.
        self.assertEqual(value_words([{}, {}], {"items": {"title": "Runbook list"}}), "2 entries")
        self.assertEqual(value_words(frozenset({"a.example"})), "a.example")
