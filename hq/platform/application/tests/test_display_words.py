"""A view model refuses, as it is built, a value a machine wrote."""

from dataclasses import is_dataclass
from datetime import date

from django.test import SimpleTestCase

import hq_sdk.ui

from ..display_words import in_words
from ..service_context import Cell
from ..ui import Insight, Kpi, ListRow


class InWordsTests(SimpleTestCase):
    def test_each_machine_shape_is_refused_and_words_are_not(self):
        for text in (
            "read 2026-10-22 09:36:47",
            "2026-10-22T09:36:47Z",
            "2026-10-22",
            "manifest to [{'doc_id': 'rb-one'}]",
            "tags ['a', 'b']",
            "{'a': 1}",
            "confirm_prune to True",
            'value="web:key" id="id_key"',
            "<input type",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                in_words(text)
        for text in (
            "read Oct 22, 9:36 AM",
            "Confirm prune to yes, manifest to 4 entries",
            "tag-True-false",
            "hq_web",
            "203 days",
        ):
            with self.subTest(text=text):
                self.assertEqual(in_words(text), text)

    def test_a_label_is_never_a_fields_name(self):
        with self.assertRaises(ValueError):
            in_words("confirm_prune", label=True)
        self.assertEqual(in_words("Confirm prune", label=True), "Confirm prune")


class ConstructionTests(SimpleTestCase):
    def test_a_view_model_refuses_machine_text_where_it_is_built(self):
        for build in (
            lambda: Kpi("Last read", value="2026-10-22T09:36:47Z"),
            lambda: ListRow("Message", meta="2026-10-22"),
            lambda: Cell("[{'name': 'web'}]"),
            lambda: Insight("good", "Tailnet", "Device", "1", "Body", facts=(("Seen", "True"),)),
        ):
            with self.assertRaisesRegex(ValueError, "say it in words"):
                build()

    def test_a_value_that_is_not_text_is_refused(self):
        for build in (
            lambda: Kpi("Enabled", value=True),
            lambda: ListRow("Message", meta=date(2026, 10, 22)),
            lambda: Cell(None),
        ):
            with self.assertRaisesRegex(ValueError, "is words"):
                build()

    def test_a_label_or_a_facts_label_is_refused_as_a_fields_name(self):
        for build in (
            lambda: Kpi("confirm_prune", value=3),
            lambda: Insight("good", "Tailnet", "Device", "1", "Body", facts=(("device_address", "192.0.2.10"),)),
        ):
            with self.assertRaises(ValueError):
                build()
        Cell("hq_web")
        Kpi("Containers", value=3)


class EveryDisplayTypeTests(SimpleTestCase):
    def test_every_display_type_a_caller_builds_checks_its_words(self):
        built = [value for value in vars(hq_sdk.ui).values() if isinstance(value, type) and is_dataclass(value)]
        self.assertIn(Kpi, built)
        for kind in (*built, Cell):
            with self.subTest(kind=kind.__qualname__):
                self.assertIn("said_in_words", kind.__post_init__.__code__.co_names)
