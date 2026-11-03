"""Facts about a subject: joined through declared keys, sourced, and never silent."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import ObservationRecord, ObservationSpec, registry
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.tests.estate_sizes import assert_subject_cost_does_not_grow

PERIMETER = "host.perimeter"


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


def dns_kinds():
    """Two provider kinds that each say what address a name answers at, from the registry."""

    first, second, *_rest = sorted(
        kind
        for kind, spec in PROVIDERS.items()
        if spec.from_record and spec.answers and spec.hostnames and spec.sample_record
    )
    return first, second


def record_for(kind, hostname, address):
    """A record of ``kind`` naming ``hostname`` at ``address``, built from its sample."""

    spec = PROVIDERS[kind]
    sample = dict(spec.sample_record or {})
    for field, value in sample.items():
        if value == next(iter(spec.hostnames(spec.from_record(sample))), None):
            sample[field] = hostname
        elif value in spec.answers(spec.from_record(sample)):
            sample[field] = address
    return sample


def labeled(facts, label):
    return [fact for fact in facts if fact.label == label]


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


class QueryCountTests(TestCase):
    """The page reads each kind once, whatever it holds or is asked about."""

    def _inventory(self, count):
        kind, other = dns_kinds()
        store(kind, *(record_for(kind, f"n{i}.example.com", f"10.0.1.{i}") for i in range(count)))
        store(other, *(record_for(other, f"n{i}.example.com", f"10.0.2.{i}") for i in range(count)))
        store(
            PERIMETER,
            *(
                {"record": "perimeter", "connection_ref": f"edge-{i}", "public_addresses": [f"198.51.100.{i}"]}
                for i in range(count)
            ),
        )
        store(
            CONTAINER_KIND,
            *({"name": f"c{i}", "host": f"host-{i}", "host_address": f"10.0.3.{i}"} for i in range(count)),
        )

    def test_the_machine_page_does_not_scale_with_records(self):
        user = get_user_model().objects.create_user("facts-op", password="x" * 20)
        self.client.force_login(user)

        def page(_count):
            response = self.client.get(reverse("control_plane:machine", kwargs={"name": "host-1"}))
            self.assertEqual(response.status_code, 200)
            return response

        assert_subject_cost_does_not_grow(self, sizes=(3, 40), arrange=self._inventory, draw=page)


class InZoneTests(TestCase):
    def test_label_boundaries_wildcards_and_spelling(self):
        from hq.domains.control_plane.names import in_zone
        from hq.domains.control_plane.provider_spec import NameContext

        self.assertTrue(in_zone("example.com", "example.com"))
        self.assertTrue(in_zone("*.Example.com.", "example.COM."))
        self.assertTrue(in_zone("a.b.example.com", "example.com"))
        self.assertFalse(in_zone("notexample.com", "example.com"))
        self.assertFalse(in_zone("example.com", ""))
        self.assertEqual(
            NameContext(hostname="a.example.com", public_zones=("example.net", "example.com")).public_zone,
            "example.com",
        )


class ConnectionFactsTests(TestCase):
    """What a connection's own readings say about it."""

    def test_a_record_speaks_for_its_own_connection_or_for_every_one(self):
        from hq.domains.control_plane.observations.adguard import DNS_KIND, FILTERING_OFF, PROTECTION_OFF

        from ..facts import connection_facts

        store(
            DNS_KIND,
            {"record": "dns", "connection_ref": "example-dns", "protection_enabled": False},
            {"record": "dns", "connection_ref": "other-dns", "filtering_enabled": False},
            {"record": "dns", "protection_enabled": False},
        )

        self.assertEqual(connection_facts("example-dns", "adguard"), ((PROTECTION_OFF, "Off"),))
        self.assertEqual(
            set(connection_facts("other-dns", "adguard")), {(PROTECTION_OFF, "Off"), (FILTERING_OFF, "Off")}
        )
        self.assertEqual(connection_facts("example-dns", "elsewhere"), ())

    def test_a_reading_that_failed_says_nothing_about_its_connection(self):
        from hq.domains.control_plane.observations.adguard import DNS_KIND

        from ..facts import connection_facts

        store(DNS_KIND, {"record": "dns", "protection_enabled": False}, reachable=False, error="refused")

        self.assertEqual(connection_facts("example-dns", "adguard"), ())


class InventoryAboutTests(TestCase):
    def test_a_kind_no_provider_reads_joins_nothing(self):
        from ..facts import Subject, inventory_about

        self.assertEqual(inventory_about("example.unknown", Subject.of(hostnames=("www.example.com",))), ())

    def test_a_record_its_provider_cannot_read_is_skipped(self):
        from ..facts import Subject, inventory_about

        kind, _other = dns_kinds()
        store(kind, {"unexpected": True}, record_for(kind, "www.example.com", "192.0.2.10"))

        joined = inventory_about(kind, Subject.of(hostnames=("www.example.com",)))

        self.assertEqual(len(joined), 1)
