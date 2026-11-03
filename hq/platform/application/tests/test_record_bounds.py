"""A record command's schema says the bounds its model holds a number to."""

from datetime import date

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from jsonschema import Draft202012Validator

from hq.domains.calendars.models import Entry
from hq.platform.application import business_use
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.integration_specs import capability_schema


class RecordBoundsTests(SimpleTestCase):
    def test_a_bounded_number_says_its_models_bounds(self):
        registry = capability_registry()
        for name in ("asset.create", "asset.update", "expense.create", "expense.update"):
            with self.subTest(name):
                field = capability_schema(registry[name])["properties"][business_use.FIELD]
                self.assertEqual((field["minimum"], field["maximum"]), (business_use.MINIMUM, business_use.MAXIMUM))

    def test_a_number_its_model_leaves_to_the_column_says_no_bound(self):
        # The column type's own range is the database's, not a rule a client works within.
        schema = capability_schema(capability_registry()["asset.create"])["properties"]
        self.assertNotIn("maximum", schema["total_cost"])


class CalendarIntervalBoundsTests(TestCase):
    """A rule a model holds as a constraint rather than a validator, said alike."""

    def test_a_repeat_interval_says_exactly_the_range_its_model_holds(self):
        field = capability_schema(capability_registry()["calendar.entry.create"])["properties"]["interval"]
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
