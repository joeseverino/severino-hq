"""A view model refuses, as it is built, a value a machine wrote."""

from dataclasses import is_dataclass
from datetime import date

from django.test import SimpleTestCase, override_settings

import hq_sdk.ui

from ..card_items import Kpi, ListRow
from ..display_words import in_words
from ..finding_model import Finding, written_fact
from ..finding_steps import ByHand, Do, Why
from ..service_context import Cell

DIGEST = "example/app:1.2@sha256:" + "0f" * 32


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
            "set it to example/app:1.2@sha256:" + "0f" * 32,
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
            lambda: Finding("example", "example", "Device", "good", "Body", evidence=(("Seen", "True"),)),
            lambda: Do(f"Pin it to {DIGEST}"),
            lambda: ByHand(f"Pin it to {DIGEST}", "example-cli up", Why.NOT_GRANTED),
        ):
            with self.assertRaisesRegex(ValueError, "say it in words"):
                build()

    @override_settings(SEVERINO_STRICT_PRINT=False)
    def test_on_a_served_page_machine_text_is_shown_and_logged_by_where_never_what(self):
        with self.assertLogs("severino.print", "ERROR") as logged:
            cell = Cell(f"Moved {DIGEST}")

        self.assertEqual(cell.text, f"Moved {DIGEST}")
        self.assertIn("Cell.text carries an image digest", logged.output[0])
        self.assertNotIn("0f0f", logged.output[0])

    def test_what_a_machine_wrote_is_drawn_as_written_in_its_own_place(self):
        from ..display_words import Code

        self.assertEqual(Do("Pin its image by its digest", value=DIGEST).value, DIGEST)
        self.assertEqual(
            ByHand("Pin it", f"example-cli pin {DIGEST}", Why.NOT_GRANTED).command, f"example-cli pin {DIGEST}"
        )
        pinned = Finding("example", "example", "Device", "good", "Body", evidence=(("Pin it as", Code(DIGEST)),))
        self.assertEqual(written_fact(*pinned.evidence[0])["code"], True)
        with self.assertRaisesRegex(ValueError, "say it in words"):
            Finding("example", "example", "Device", "good", "Body", evidence=(("Pin it as", DIGEST),))

    def test_a_readout_is_shown_as_it_arrived(self):
        """A header, an address or an identifier is a record's own text, whatever it holds."""

        value = '"Chromium";v="159", "Not)A;Brand";v="24"'
        self.assertEqual(Cell(value, readout=True).text, value)
        with self.assertRaisesRegex(ValueError, "say it in words"):
            Cell(value)
        with self.assertRaisesRegex(ValueError, "is words"):
            Cell(None, readout=True)

    def test_a_value_that_is_not_text_is_refused(self):
        for build in (
            lambda: Kpi("Enabled", value=True),
            lambda: Kpi("Open", value=3),
            lambda: ListRow("Message", meta=date(2026, 10, 22)),
            lambda: Cell(None),
        ):
            with self.assertRaisesRegex(ValueError, "is words"):
                build()

    def test_a_label_or_a_facts_label_is_refused_as_a_fields_name(self):
        for build in (
            lambda: Kpi("confirm_prune", value="Off"),
            lambda: Finding(
                "example", "example", "Device", "good", "Body", evidence=(("device_address", "192.0.2.10"),)
            ),
        ):
            with self.assertRaises(ValueError):
                build()
        Cell("hq_web")
        Kpi("Containers", value="Running")


class EveryDisplayTypeTests(SimpleTestCase):
    def test_every_display_type_a_caller_builds_checks_its_words(self):
        built = [value for value in vars(hq_sdk.ui).values() if isinstance(value, type) and is_dataclass(value)]
        self.assertIn(Kpi, built)
        for kind in (*built, Cell):
            with self.subTest(kind=kind.__qualname__):
                self.assertIn("said_in_words", kind.__post_init__.__code__.co_names)
