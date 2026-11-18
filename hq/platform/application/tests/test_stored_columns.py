"""A command field a model stores is held to its column's rules as the
command is validated, and its schema says them."""

from django.test import SimpleTestCase
from pydantic import ValidationError

from ..assets import AssetCommand
from ..calendar_entries import EntryCommand
from ..infrastructure import ManagedResourceCommand
from ..integration_specs import command_adapter, command_schema

ZONE = {"zone": "example.com", "connection_ref": "example-dns"}


def refused(command_type, payload):
    """``(type, field)`` of each error validating ``payload`` raises."""

    try:
        command_adapter(command_type).validate_python(payload)
    except ValidationError as error:
        return {(item["type"], item["loc"][0]) for item in error.errors()}
    return set()


class StoredColumnTests(SimpleTestCase):
    def test_a_value_its_column_refuses_is_refused_as_the_command_is_validated(self):
        for command_type, payload, expected in (
            (ManagedResourceCommand, {"kind": "cloudflare.zone", "key": "", "spec": ZONE}, ("string_too_short", "key")),
            (
                ManagedResourceCommand,
                {"kind": "cloudflare.zone", "key": "not a key", "spec": ZONE},
                ("string_pattern_mismatch", "key"),
            ),
            (
                ManagedResourceCommand,
                {"kind": "cloudflare.zone", "key": "k" * 181, "spec": ZONE},
                ("string_too_long", "key"),
            ),
            (AssetCommand, {"item_name": ""}, ("string_too_short", "item_name")),
            (AssetCommand, {"item_name": "x", "slug": "a b"}, ("string_pattern_mismatch", "slug")),
            (
                AssetCommand,
                {"item_name": "x", "business_use_percentage": 101},
                ("less_than_equal", "business_use_percentage"),
            ),
            (AssetCommand, {"item_name": "x", "total_cost": "12345678901.5"}, ("decimal_whole_digits", "total_cost")),
            (EntryCommand, {"title": "", "starts_on": "2026-07-01"}, ("string_too_short", "title")),
        ):
            with self.subTest(payload=payload):
                self.assertIn(expected, refused(command_type, payload))

    def test_an_empty_value_the_save_fills_is_taken(self):
        # A slug left empty is derived from the name, as the column's own form allows.
        self.assertEqual(refused(AssetCommand, {"item_name": "x", "slug": ""}), set())

    def test_a_field_held_in_another_shape_than_its_column_is_left_to_the_save(self):
        from ..stored_columns import stored_rules

        # The weekdays are a list the save joins into the column's text.
        self.assertNotIn("weekdays", dict(stored_rules(EntryCommand)))
        self.assertEqual(refused(EntryCommand, {"title": "x", "starts_on": "2026-07-01", "weekdays": [0, 2]}), set())

    def test_a_rule_between_columns_is_said_on_the_field_it_is_reported_beside(self):
        from hq.domains.calendars.models import Entry

        ends_at = command_schema(EntryCommand)["properties"]["ends_at"]
        between = [rule for rule in Entry._meta.constraints if rule.name == "calendar_entry_end_time_follows_start"]

        self.assertEqual(ends_at["description"], "The time it ends.")
        self.assertIn(str(between[0].violation_error_message), ends_at.get("x-hq-rules", []))

    def test_the_schema_says_each_rule_beside_the_fields_own_type(self):
        key = command_schema(ManagedResourceCommand)["properties"]["key"]
        self.assertEqual(
            {name: key[name] for name in ("$ref", "maxLength", "minLength", "pattern")},
            {"$ref": "#/$defs/ManagedResourceKey", "maxLength": 180, "minLength": 1, "pattern": "^[-a-zA-Z0-9_]+$"},
        )
        # A shared type's definition stays its own, whatever column a field of it is stored in.
        slug = command_schema(AssetCommand)["$defs"]["Slug"]
        self.assertNotIn("maxLength", slug)
