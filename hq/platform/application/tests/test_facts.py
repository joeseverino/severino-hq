"""Facts about a subject: joined through declared keys, sourced, and never silent."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import ObservationRecord, ObservationSpec, registry


def store(kind, *records, reachable=True, error="", age=timedelta(0)):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": reachable,
            "error": error,
            "observed_at": timezone.now() - age,
        },
    )


class UnreadableTests(TestCase):
    def test_a_refused_kind_that_cannot_join_the_subject_says_nothing_about_it(self):
        """The "Not readable" banner belongs only where the kind could change the page."""

        from ..facts import Subject, unreadable_labels

        class Named(ObservationRecord):
            name: str

        by_name = ObservationSpec(
            "example.by_name",
            "example",
            "By name",
            Named,
            hostnames=lambda record: (record.get("name", ""),),
        )
        by_address = ObservationSpec(
            "example.by_address",
            "example",
            "By address",
            Named,
            addresses=lambda record: (record.get("name", ""),),
        )
        joins_nothing = ObservationSpec("example.nothing", "example", "Nothing", Named)
        for spec in (by_name, by_address, joins_nothing):
            store(spec.kind, reachable=False, error="refused")

        with mock.patch("hq.platform.application.facts.OBSERVATIONS", registry((by_name, by_address, joins_nothing))):
            service = unreadable_labels(Subject.of(hostnames=("app.example.com",)))
            address = unreadable_labels(Subject.of(addresses=("192.0.2.10",)))
            every = unreadable_labels()

        self.assertIn("By name", service)
        self.assertNotIn("By address", service)
        self.assertNotIn("Nothing", service)
        self.assertIn("By address", address)
        self.assertNotIn("By name", address)
        self.assertEqual({"By name", "By address", "Nothing"} - set(every), set())


class InventoryAboutTests(TestCase):
    def test_a_kind_no_provider_reads_joins_nothing(self):
        from ..facts import Subject, inventory_about

        self.assertEqual(inventory_about("example.unknown", Subject.of(hostnames=("www.example.com",))), ())
