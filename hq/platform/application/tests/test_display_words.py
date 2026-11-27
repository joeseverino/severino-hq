"""A view model refuses, as it is built, a value a machine wrote."""

from django.test import SimpleTestCase, override_settings

from ..display_words import in_words
from ..service_context import Cell

DIGEST = "example/app:1.2@sha256:" + "0f" * 32


class InWordsTests(SimpleTestCase):
    def test_a_label_is_never_a_fields_name(self):
        with self.assertRaises(ValueError):
            in_words("confirm_prune", label=True)


class ConstructionTests(SimpleTestCase):
    @override_settings(SEVERINO_STRICT_PRINT=False)
    def test_on_a_served_page_machine_text_is_shown_and_logged_by_where_never_what(self):
        with self.assertLogs("severino.print", "ERROR") as logged:
            cell = Cell(f"Moved {DIGEST}")

        self.assertEqual(cell.text, f"Moved {DIGEST}")
        self.assertNotIn("0f0f", logged.output[0])

    def test_a_readout_is_shown_as_it_arrived(self):
        """A header, an address or an identifier is a record's own text, whatever it holds."""

        value = '"Chromium";v="159", "Not)A;Brand";v="24"'
        self.assertEqual(Cell(value, readout=True).text, value)
        with self.assertRaises(ValueError):
            Cell(value)
        with self.assertRaises(ValueError):
            Cell(None, readout=True)
