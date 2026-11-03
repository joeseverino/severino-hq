"""Every command that writes to a record it targets, with a command a create
also takes, is told which fields its caller sent, says what it keeps, and so
requires none of the fields it keeps."""

from django.test import SimpleTestCase

from ..capabilities import capability_registry
from ..domains import host_records
from ..integration_specs import capability_schema, kept_by, takes_sent_fields
from ..records import KEPT


def _keeping_specs():
    """Every command that writes to a record it targets and shares its command
    with one that creates: each record domain's update and upsert, and any
    other targeted command whose command a create also takes."""

    registry = capability_registry()
    creating = {spec.command_type for spec in registry.values() if not spec.target_kind}
    names = {f"{records.noun}.{verb}" for records in host_records() for verb in ("update", "upsert")}
    return [
        spec
        for spec in registry.values()
        if spec.name in names or (spec.target_kind and spec.command_type in creating and spec.effect != "read")
    ]


class EveryUpdateIsToldWhatWasSentTests(SimpleTestCase):
    def test_every_record_update_and_upsert_is_told_what_was_sent(self):
        specs = _keeping_specs()
        self.assertIn("calendar.entry.update", {spec.name for spec in specs})
        for spec in specs:
            with self.subTest(spec.name):
                self.assertTrue(takes_sent_fields(spec.handler), f"{spec.name} would reset what it was not sent")

    def test_every_targeted_write_told_what_was_sent_says_what_it_keeps_and_no_other(self):
        for spec in capability_registry().values():
            if not spec.target_kind:
                continue
            with self.subTest(spec.name):
                self.assertEqual(
                    kept_by(spec) is not None,
                    takes_sent_fields(spec.handler),
                    f"{spec.name} needs a field it could keep, or writes back what it was not sent",
                )

    def test_an_update_says_a_field_left_out_keeps_its_value_requires_none_it_keeps_and_shows_no_default(self):
        for spec in capability_registry().values():
            if (kept := kept_by(spec)) is None:
                continue
            with self.subTest(spec.name):
                self.assertIn(KEPT, spec.summary)
                schema = capability_schema(spec)
                self.assertLessEqual(set(schema.get("required", ())), kept.always_sent)
                properties = schema["properties"]
                self.assertEqual([name for name, field in properties.items() if "default" in field], [])
