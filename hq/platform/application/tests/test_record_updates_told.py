"""Every command that writes to a record it targets, with fields a create also
takes, is told which fields its caller sent, says what it keeps, and so
requires none of the fields it keeps."""

from unittest import mock

from django.test import SimpleTestCase
from pydantic import BaseModel

from ..capabilities import capability_registry
from ..domains import host_records
from ..integration_specs import CapabilitySpec, capability_schema, command_schema, kept_by, takes_sent_fields
from ..records import KEPT


def _keeping_specs():
    """Every command that writes to a record it targets and shares its fields
    with one that creates: each record domain's update and upsert, and any
    other targeted command whose command a create also takes. A command with
    no fields has nothing to reset, whatever else takes it."""

    registry = capability_registry()
    creating = {
        spec.command_type
        for spec in registry.values()
        if not spec.target_kind and command_schema(spec.command_type).get("properties")
    }
    names = {f"{records.noun}.{verb}" for records in host_records() for verb in ("update", "upsert")}
    return [
        spec
        for spec in registry.values()
        if spec.name in names or (spec.target_kind and spec.command_type in creating and spec.effect != "read")
    ]


class _Nothing(BaseModel):
    pass


class _Named(BaseModel):
    name: str = ""


def _spec(name: str, command_type: type, target_kind: str | None) -> CapabilitySpec:
    return CapabilitySpec(name, name, "write", "example.write", command_type, lambda _command: {}, target_kind)


class KeepingSelectionTests(SimpleTestCase):
    def test_a_targeted_command_is_held_only_when_it_shares_fields_a_create_takes(self):
        registry = {
            spec.name: spec
            for spec in (
                _spec("example.thing.start", _Nothing, None),
                _spec("example.thing.stop", _Nothing, "key"),
                _spec("example.thing.add", _Named, None),
                _spec("example.thing.rename", _Named, "key"),
            )
        }
        with mock.patch(f"{__name__}.capability_registry", return_value=registry):
            held = {spec.name for spec in _keeping_specs()}
        self.assertIn("example.thing.rename", held)
        self.assertNotIn("example.thing.stop", held)


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
