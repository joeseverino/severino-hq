"""The reading contract: one registration per kind, and only its fields kept."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from application.inventory import record_inventory
from application.security import cli_principal
from control_plane.models import ProviderInventory
from control_plane.observations import OBSERVATIONS, ObservationRecord, ObservationSpec, registry


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

    def test_a_kind_is_registered_once(self):
        spec = ObservationSpec("example.thing", "example", "Thing", Named)
        with self.assertRaises(ValueError):
            registry((spec, spec))

    def test_every_reading_has_a_reader_and_every_reader_a_home(self):
        from controller_runtime.providers import PROVIDER_INVENTORY

        from control_plane.providers import PROVIDERS

        controller_read = {
            kind for kind, spec in OBSERVATIONS.items() if spec.read_by == "controller"
        }
        self.assertEqual(sorted(controller_read - set(PROVIDER_INVENTORY)), [])
        self.assertEqual(
            sorted(set(PROVIDER_INVENTORY) - set(PROVIDERS) - controller_read), []
        )
        self.assertEqual(sorted(set(OBSERVATIONS) & set(PROVIDERS)), [])

    def test_readers_and_readings_are_the_same_set(self):
        from application.public_registry import READERS
        from controller_runtime.providers import OBSERVATION_READERS

        self.assertEqual(
            sorted(OBSERVATION_READERS),
            sorted(k for k, s in OBSERVATIONS.items() if s.read_by == "controller"),
        )
        self.assertEqual(
            sorted(READERS),
            sorted(k for k, s in OBSERVATIONS.items() if s.read_by == "hq"),
        )

    def test_a_facet_and_a_reader_are_from_their_vocabularies(self):
        with self.assertRaises(ValueError):
            registry((ObservationSpec("example.thing", "example", "Thing", Named, facet="x"),))
        with self.assertRaises(ValueError):
            registry((ObservationSpec("example.thing", "example", "Thing", Named, read_by="x"),))


class IngestTests(TestCase):
    def _sweep(self, records):
        record_inventory(
            {"host.perimeter": {"ok": True, "records": records}},
            principal=cli_principal(),
        )
        return ProviderInventory.objects.get(kind="host.perimeter")

    def test_a_stored_reading_keeps_only_its_schema(self):
        stored = self._sweep([
            {"record": "perimeter", "connection_ref": "example-edge",
             "answered_publicly": [443], "token": "never stored"},
        ])

        self.assertNotIn("token", stored.records[0])
        self.assertEqual(stored.records[0]["answered_publicly"], [443])

    def test_refused_records_are_said_rather_than_silently_lost(self):
        stored = self._sweep([{"record": "perimeter"}])

        self.assertEqual(stored.records, [])
        self.assertIn("did not match", stored.error)


class NotConnectedIngestTests(TestCase):
    def test_not_connected_is_stored(self):
        record_inventory(
            {"host.perimeter": {"ok": True, "records": [], "connected": False}},
            principal=cli_principal(),
        )

        stored = ProviderInventory.objects.get(kind="host.perimeter")
        self.assertFalse(stored.connected)
        self.assertTrue(stored.reachable)
