"""A value read the way a person reads it: the words every held request,
audit entry and command result is described in."""

from datetime import UTC, date, datetime, timedelta

import time_machine
from django.test import SimpleTestCase
from django.utils import timezone

from ..labels import field_label
from ..moments import when_exact
from ..payload_words import SAID_WHOLE, value_words
from ..printable import Unprintable


class ValueWordsTests(SimpleTestCase):
    def test_each_shape_reads_as_words(self):
        self.assertEqual(value_words(True), "yes")
        self.assertEqual(value_words(["a.example", "b.example"]), "a.example, b.example")
        self.assertEqual(value_words([{"enabled": True}, {"enabled": False}]), "2 entries")
        self.assertEqual(value_words({"created": 3, "dns_name": "a.example"}), "created 3, DNS name a.example")
        self.assertEqual(value_words({"spec": {"a": 1}}), "1 field")
        self.assertEqual(value_words(None), "")

    def test_a_moment_reads_in_full_and_an_object_is_refused(self):
        self.assertEqual(value_words("2026-10-03"), when_exact(date(2026, 10, 3)))
        self.assertEqual(value_words(date(2026, 10, 3)), when_exact(date(2026, 10, 3)))
        self.assertEqual(value_words("2026-10-03T09:29:00Z"), when_exact(datetime(2026, 10, 3, 9, 29, tzinfo=UTC)))
        for text in ("0001-01-01T00:00:00", "2026-13-45", "20261003"):
            with self.subTest(text):
                self.assertEqual(value_words(text), text)
        with self.assertRaisesMessage(Unprintable, "value_words printed a builtins.object"):
            value_words(object())

    def test_a_moment_reads_the_same_however_long_after_it_is_worded(self):
        # The words are stored, so the ones written now still hold a year on.
        moment = timezone.now() - timedelta(seconds=5)
        written = value_words(moment)
        with time_machine.travel(moment + timedelta(days=400)):
            self.assertEqual(value_words(moment), written)

    def test_a_record_is_said_whole_only_while_it_is_small_and_plain(self):
        self.assertEqual(value_words({"enabled": False, "note": None}), "enabled no, note empty")
        large = {f"field_{index}": index for index in range(SAID_WHOLE + 1)}
        self.assertEqual(value_words(large), f"{SAID_WHOLE + 1} fields")

    def test_a_list_is_counted_by_what_its_schema_calls_an_item(self):
        self.assertEqual(value_words([{}, {}], {"items": {"title": "Runbook"}}), "2 runbooks")
        self.assertEqual(value_words(({},), {"anyOf": [{"type": "null"}, {"items": {"title": "Zone"}}]}), "1 zone")
        # A title of several words names the list rather than one item of it.
        self.assertEqual(value_words([{}, {}], {"items": {"title": "Runbook list"}}), "2 entries")
        self.assertEqual(value_words(frozenset({"a.example"})), "a.example")

    def test_a_field_reads_by_its_title_or_its_name_as_a_sentence(self):
        self.assertEqual(field_label("confirm_prune"), "Confirm prune")
        self.assertEqual(field_label("dns_name"), "DNS name")
        self.assertEqual(field_label("doc_id", "Document"), "Document")


def _grant(source: str, *, via: str = "") -> dict:
    return {"src": [source], "dst": ["tag:example"], "ip": ["443"], **({"via": [via]} if via else {})}


class ChangedWordsTests(SimpleTestCase):
    """Entries of a list of records are matched by content, as ``git diff`` matches lines."""

    def changes(self, before, after) -> dict[str, tuple[str, str, str]]:
        from ..payload_words import changed_words

        return {row.label: (row.change, row.before, row.after) for row in changed_words(before, after)}

    def test_an_entry_inserted_mid_list_is_one_entry_added(self):
        grants = [_grant(f"group:example-{index}", via="tag:exit") for index in range(8)]
        inserted = [*grants[:4], _grant("group:admins"), *grants[4:]]

        said = self.changes({"grants": grants}, {"grants": inserted})

        self.assertEqual(list(said), ["Grants › group:admins"])
        change, before, after = said["Grants › group:admins"]
        self.assertEqual((change, before), ("added", ""))
        self.assertIn("group:admins", after)

    def test_an_entry_removed_mid_list_is_one_entry_removed(self):
        grants = [_grant(f"group:example-{index}") for index in range(6)]

        said = self.changes({"grants": grants}, {"grants": [*grants[:2], *grants[3:]]})

        self.assertEqual([(label, row[0]) for label, row in said.items()], [("Grants › group:example-2", "removed")])

    def test_an_entry_edited_beside_an_insert_is_said_field_by_field_where_it_now_stands(self):
        tests = [{"src": f"user@example-{index}.com", "accept": ["tag:example:443"]} for index in range(4)]
        edited = [*tests[:1], {"src": "group:admins", "accept": ["tag:admin:22"]}, *tests[1:]]
        edited[3] = {**tests[2], "accept": ["tag:example:443", "tag:example:80"]}

        said = self.changes({"tests": tests}, {"tests": edited})

        self.assertEqual(said["Tests › group:admins"][0], "added")
        self.assertEqual(
            said["Tests › user@example-2.com › Accept"],
            ("changed", "tag:example:443", "tag:example:443, tag:example:80"),
        )
        self.assertEqual(len(said), 2)

    def test_an_entry_replaced_by_one_unlike_it_reads_as_its_place_changing(self):
        before = [_grant("group:example"), {"name": "example", "port": 1}]
        after = [_grant("group:example"), {"other": True}]

        said = self.changes({"items": before}, {"items": after})

        self.assertEqual([(label, row[0]) for label, row in said.items()], [("Items › Entry 2", "changed")])

    def test_a_long_list_of_alike_entries_still_matches_by_content(self):
        # Past two hundred entries, SequenceMatcher's own heuristic would treat a
        # common entry as junk and match nothing.
        grants = [_grant("group:example") for _ in range(250)]

        said = self.changes({"grants": grants}, {"grants": [_grant("group:admins"), *grants]})

        self.assertEqual([(label, row[0]) for label, row in said.items()], [("Grants › group:admins", "added")])

    def test_an_entry_is_named_by_its_first_field_that_holds_text(self):
        locations = [
            {"forward_port": 80, "path": "/", "forward_host": "192.0.2.10"},
            {"forward_port": 80, "path": "/api", "forward_host": "192.0.2.11"},
        ]
        moved = [locations[0], {**locations[1], "forward_host": "203.0.113.5"}]

        said = self.changes({"locations": locations}, {"locations": moved})

        self.assertEqual(said, {"Locations › /api › Forward host": ("changed", "192.0.2.11", "203.0.113.5")})

    def test_entries_that_share_a_name_are_named_by_where_they_stand(self):
        grants = [_grant("group:example"), _grant("group:example")]
        moved = [grants[0], {**grants[1], "dst": ["tag:example", "tag:other"]}]

        said = self.changes({"grants": grants}, {"grants": moved})

        self.assertEqual(list(said), ["Grants › Entry 2 › Dst"])


class ListWordsTests(SimpleTestCase):
    def test_a_list_of_records_is_said_by_its_first_names_and_a_count_of_the_rest(self):
        docs = [{"doc_id": f"rb-example-{index}", "title": "An example runbook"} for index in range(12)]

        said = value_words(docs)

        self.assertTrue(said.startswith("rb-example-0, rb-example-1"), said)
        self.assertIn("9 more", said)
        self.assertNotIn("rb-example-11", said)
        self.assertEqual(value_words(docs[:2]), "rb-example-0, rb-example-1")
