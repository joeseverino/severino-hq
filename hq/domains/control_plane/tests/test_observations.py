"""The reading contract: one registration per kind, and only its fields kept."""

import json
import re

from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import (
    OBSERVATIONS,
    ObservationRecord,
    ObservationSpec,
    Stamp,
)
from hq.platform.application.inventory import record_inventory
from hq.platform.application.tests.sentinel import SENTINEL


class Named(ObservationRecord):
    name: str


class ContractTests(SimpleTestCase):
    def test_fields_the_schema_does_not_name_are_dropped(self):
        spec = ObservationSpec("example.thing", "example", "Thing", Named)

        kept, refused = spec.clean([{"name": "a", "client_secret": "s3cret"}])

        self.assertEqual(kept, [{"name": "a"}])
        self.assertEqual(refused, 0)

    def test_a_record_that_does_not_fit_is_refused_and_counted(self):
        spec = ObservationSpec("example.thing", "example", "Thing", Named)

        kept, refused = spec.clean([{"other": 1}, "not a record", {"name": "b"}])

        self.assertEqual(kept, [{"name": "b"}])
        self.assertEqual(refused, 2)

    def test_hq_readers_and_the_readings_on_its_clock_are_the_same_set(self):
        from hq.platform.application.public_registry import READERS

        self.assertEqual(
            sorted(READERS),
            sorted(k for k, s in OBSERVATIONS.items() if s.read_by == "hq" and not s.on_open),
        )

    def test_no_record_schema_carries_an_unread_marker_from_a_credential_read(self):
        # A part a credential could not read is a part refusal, never a record
        # field. Only keyless registry lookups keep a negative answer per record.
        for kind, spec in OBSERVATIONS.items():
            if spec.read_by == "controller":
                with self.subTest(kind=kind):
                    self.assertNotIn("unread", spec.record.model_fields)


class Stamped(ObservationRecord):
    seen_at: Stamp = None


_STAMP_NAME = re.compile(r"_at\b|stamp|created|updated|expires|last_seen|modified|_date|date_")


def _records(base: type[ObservationRecord]):
    for record in base.__subclasses__():
        yield record
        yield from _records(record)


class StampTests(SimpleTestCase):
    def test_a_stamp_is_stored_as_iso_in_utc_whatever_its_provider_wrote(self):
        spec = ObservationSpec("example.thing", "example", "Thing", Stamped)

        kept, refused = spec.clean([{"seen_at": "2026-10-22T09:36:47-04:00"}, {"seen_at": "2026-10-22 13:36:47"}])

        self.assertEqual(kept, [{"seen_at": "2026-10-22T13:36:47+00:00"}] * 2)
        self.assertEqual(refused, 0)

    def test_what_names_no_instant_is_stored_as_none_of_one(self):
        spec = ObservationSpec("example.thing", "example", "Thing", Stamped)

        kept, _refused = spec.clean([{"seen_at": "soon"}, {"seen_at": "0001-01-01T00:00:00Z"}, {}])

        self.assertEqual(kept, [{"seen_at": ""}, {"seen_at": ""}, {}])

    def test_no_record_holds_a_stamp_as_bare_text(self):
        self.assertTrue(OBSERVATIONS)
        for record in _records(ObservationRecord):
            for name, field in record.model_fields.items():
                if _STAMP_NAME.search(name):
                    with self.subTest(record=record.__qualname__, field=name):
                        self.assertFalse(
                            field.annotation is str and not field.metadata,
                            f"{record.__qualname__}.{name} is a stamp: type it Stamp.",
                        )


class IngestTests(TestCase):
    def _sweep(self, records):
        record_inventory({"host.perimeter": {"ok": True, "records": records}})
        return ProviderInventory.objects.get(kind="host.perimeter")

    def test_a_stored_reading_keeps_only_its_schema(self):
        stored = self._sweep(
            [
                {
                    "record": "perimeter",
                    "connection_ref": "example-edge",
                    "answered_publicly": [443],
                    "token": SENTINEL,
                },
            ]
        )

        self.assertNotIn(SENTINEL, json.dumps(stored.records))
        self.assertEqual(stored.records[0]["answered_publicly"], [443])

    def test_refused_records_are_said_rather_than_silently_lost(self):
        stored = self._sweep([{"record": "perimeter"}])

        self.assertEqual(stored.records, [])
        self.assertIn("did not match", stored.error)


class NotConnectedIngestTests(TestCase):
    def test_not_connected_is_stored(self):
        record_inventory({"host.perimeter": {"ok": True, "records": [], "connected": False}})

        stored = ProviderInventory.objects.get(kind="host.perimeter")
        self.assertFalse(stored.connected)
        self.assertTrue(stored.reachable)
