"""A record command's schema says the bounds its model holds a number to."""

from datetime import date

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from jsonschema import Draft202012Validator

from hq.domains.calendars.models import Entry
from hq.platform.application import business_use
from hq.platform.application.capabilities import capability_registry, command_spec
from hq.platform.application.integration_specs import capability_schema
from hq.platform.application.json_schemas import fields


class RecordBoundsTests(SimpleTestCase):
    def test_a_bounded_number_says_its_models_bounds(self):
        bounded = {
            spec.name: field
            for spec in capability_registry()
            if (field := fields(capability_schema(spec)).get(business_use.FIELD))
        }
        self.assertTrue(bounded, f"no command takes {business_use.FIELD}")
        for name, field in bounded.items():
            with self.subTest(name):
                self.assertEqual(
                    (field.get("minimum"), field.get("maximum")), (business_use.MINIMUM, business_use.MAXIMUM)
                )

    def test_a_number_its_model_leaves_to_the_column_says_no_bound(self):
        # The column type's own range is the database's, not a rule a client works within.
        schema = fields(capability_schema(command_spec("asset.create")))
        self.assertNotIn("maximum", schema["total_cost"])


class CalendarIntervalBoundsTests(TestCase):
    """A rule a model holds as a constraint rather than a validator, said alike."""

    def test_a_repeat_interval_says_exactly_the_range_its_model_holds(self):
        field = fields(capability_schema(command_spec("calendar.entry.create")))["interval"]
        low, high = field["minimum"], field["maximum"]
        for interval in (low - 1, low, high, high + 1):
            with self.subTest(interval=interval):
                said = Draft202012Validator(field).is_valid(interval)
                entry = Entry(title="Example", starts_on=date(2026, 7, 1), repeat="daily", interval=interval)
                try:
                    entry.validate_constraints()
                    held = True
                except ValidationError:
                    held = False
                self.assertEqual(said, held)

    def test_a_title_says_exactly_what_its_model_refuses(self):
        field = fields(capability_schema(command_spec("calendar.entry.create")))["title"]
        for title in ("", " ", "\t", "\x00", " \x00a", "a\x00", " a ", "x" * 160, "x" * 161):
            with self.subTest(title=title):
                said = Draft202012Validator(field).is_valid(title)
                try:
                    Entry(title=title, starts_on=date(2026, 7, 1)).full_clean()
                    held = True
                except ValidationError as error:
                    held = "title" not in error.message_dict
                self.assertEqual(said, held)
