"""A view model refuses, as it is built, a value a machine wrote."""

from django.test import SimpleTestCase, override_settings

from ..display_words import in_words
from ..finding_model import Finding
from ..service_context import Cell
from ..tone import Tone

DIGEST = "example/app:1.2@sha256:" + "0f" * 32
# What a host or a registry hands HQ, in shapes a page refuses.
ARRIVED = ("The host did not apply it: {'apps': [{'health': 'healthy'}]}", f"Moved {DIGEST}")


class InWordsTests(SimpleTestCase):
    def test_a_label_is_never_a_fields_name(self):
        with self.assertRaises(ValueError):
            in_words("confirm_prune", "Example", label=True)


class ConstructionTests(SimpleTestCase):
    @override_settings(SEVERINO_STRICT_PRINT=False)
    def test_on_a_served_page_what_hq_did_not_write_is_shown_and_logged_by_where_never_what(self):
        for text in ARRIVED:
            with self.subTest(text), self.assertLogs("severino.print", "ERROR") as logged:
                cell = Cell(text)
                finding = Finding("example", "", text, Tone.SERIOUS, "Example.", evidence=(("Example", text),))
            self.assertEqual((cell.text, finding.title, finding.evidence[0][1]), (text, text, text))
            self.assertNotIn(text, "".join(logged.output))
        with self.subTest("not text"), self.assertLogs("severino.print", "ERROR"):
            Cell(None, readout=True)

    def test_a_readout_is_shown_as_it_arrived(self):
        """A header, an address or an identifier is a record's own text, whatever it holds."""

        value = '"Chromium";v="159", "Not)A;Brand";v="24"'
        self.assertEqual(Cell(value, readout=True).text, value)
        with self.assertRaises(ValueError):
            Cell(value)
        with self.assertRaises(ValueError):
            Cell(None, readout=True)
