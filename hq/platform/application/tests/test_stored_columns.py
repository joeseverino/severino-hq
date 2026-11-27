"""A command field whose column rule the save satisfies is left to the save."""

from django.test import SimpleTestCase
from pydantic import ValidationError

from ..assets import AssetCommand
from ..calendar_entries import EntryCommand
from ..integration_specs import command_adapter
from ..stored_columns import stored_rules


def refused(command_type, payload):
    """``(type, field)`` of each error validating ``payload`` raises."""

    try:
        command_adapter(command_type).validate_python(payload)
    except ValidationError as error:
        return {(item["type"], item["loc"][0]) for item in error.errors()}
    return set()


class StoredColumnTests(SimpleTestCase):
    def test_an_empty_value_the_save_fills_is_taken(self):
        # A slug left empty is derived from the name, as the column's own form allows.
        self.assertEqual(refused(AssetCommand, {"item_name": "x", "slug": ""}), set())

    def test_a_field_held_in_another_shape_than_its_column_is_left_to_the_save(self):
        # The weekdays are a list the save joins into the column's text.
        self.assertNotIn("weekdays", dict(stored_rules(EntryCommand)))
        self.assertEqual(refused(EntryCommand, {"title": "x", "starts_on": "2026-07-01", "weekdays": [0, 2]}), set())
