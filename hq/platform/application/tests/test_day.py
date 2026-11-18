from datetime import date

from django.test import SimpleTestCase
from pydantic import TypeAdapter, ValidationError

from hq.platform.application.command_fields import Day


class DayTests(SimpleTestCase):
    def test_a_day_is_the_iso_date_its_schema_says_and_nothing_pydantic_would_read_as_one(self):
        days = TypeAdapter(Day | None)

        self.assertEqual(days.json_schema()["anyOf"][0], {"format": "date", "type": "string"})
        for sent, taken in (("2026-11-18", date(2026, 11, 18)), (date(2026, 11, 18), date(2026, 11, 18)), (None, None)):
            with self.subTest(sent=sent):
                self.assertEqual(days.validate_python(sent), taken)
        for sent in (0, 1_700_000_000, 0.0, "0", "1700000000", "20261118", " 2026-11-18", "2026-11-18\n", True):
            with self.subTest(sent=sent), self.assertRaises(ValidationError):
                days.validate_python(sent)
