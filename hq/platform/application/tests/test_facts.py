"""Facts about a subject: joined through declared keys, sourced, and never silent."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS, ObservationRecord, ObservationSpec, registry
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow, assert_subject_cost_does_not_grow

from ..facts import OBSERVED, STALE, UNREADABLE, facts_about
from ..projection import projection_scope

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


class JoinTests(TestCase):
    def test_a_reading_joins_by_address(self):
        store(
            PERIMETER,
            {"record": "perimeter", "connection_ref": "example-edge", "public_addresses": ["198.51.100.7"]},
            {"record": "perimeter", "connection_ref": "other-edge", "public_addresses": ["198.51.100.8"]},
        )

        facts = facts_about((), ("198.51.100.7",))

        addresses = labeled(facts, "Address")
        self.assertEqual([f.value for f in addresses], ["198.51.100.7"])
        self.assertEqual(addresses[0].source_kind, PERIMETER)
        self.assertEqual(addresses[0].source_label, OBSERVATIONS[PERIMETER].label)
        self.assertEqual(addresses[0].connection_ref, "example-edge")
        self.assertEqual(addresses[0].state, OBSERVED)
        self.assertIsNotNone(addresses[0].observed_at)

    def test_a_provider_record_joins_by_hostname(self):
        kind, _other = dns_kinds()
        store(
            kind,
            record_for(kind, "www.example.com", "192.0.2.10"),
            record_for(kind, "elsewhere.example.org", "192.0.2.99"),
        )

        facts = facts_about(("WWW.example.com.",), ())

        self.assertEqual([f.value for f in labeled(facts, "Address")], ["192.0.2.10"])
        self.assertEqual({f.source_kind for f in facts}, {kind})

    def test_a_container_joins_by_the_host_it_runs_on(self):
        store(
            CONTAINER_KIND,
            {
                "name": "web",
                "host": "example-host",
                "host_address": "10.0.0.5",
                "state": "running",
                "connection_ref": "example-portainer",
            },
            {"name": "db", "host": "other-host", "state": "running"},
        )

        facts = facts_about(("example-host",), ())

        self.assertEqual([f.value for f in labeled(facts, "Container")], ["web"])
        self.assertEqual([f.value for f in labeled(facts, "Address")], ["10.0.0.5"])

    def test_nothing_joins_a_subject_with_no_keys(self):
        store(PERIMETER, reachable=False, error="refused")

        self.assertEqual(facts_about((), ()), ())


class UnreadableTests(TestCase):
    def test_an_unreachable_kind_is_one_fact_with_its_reason_and_requires(self):
        class Named(ObservationRecord):
            name: str

        spec = ObservationSpec(
            "example.reading",
            "example",
            "Example reading",
            Named,
            requires=("Zone Read",),
            hostnames=lambda record: (record.get("name", ""),),
        )
        store("example.reading", reachable=False, error="403 from the provider.")

        with mock.patch("hq.platform.application.facts.OBSERVATIONS", registry((spec,))):
            facts = facts_about(("host.example.com",), ())

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].state, UNREADABLE)
        self.assertIn("403 from the provider.", facts[0].detail)
        self.assertIn("Zone Read", facts[0].detail)

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
            facts = facts_about((), ("192.0.2.10",))

        self.assertIn("By name", service)
        self.assertNotIn("By address", service)
        self.assertNotIn("Nothing", service)
        self.assertIn("By address", address)
        self.assertNotIn("By name", address)
        self.assertEqual({"By name", "By address", "Nothing"} - set(every), set())
        self.assertEqual({f.label for f in facts if f.state == UNREADABLE}, {"By address"})

    def test_an_unreachable_inventory_kind_is_not_silence(self):
        kind, _other = dns_kinds()
        store(kind, reachable=False, error="connection refused")

        facts = facts_about(("www.example.com",), ())

        self.assertEqual([(f.source_kind, f.state) for f in facts], [(kind, UNREADABLE)])
        self.assertIn("connection refused", facts[0].detail)

    def test_a_partial_read_names_the_part_it_could_not_read(self):
        store(
            PERIMETER,
            {
                "record": "perimeter",
                "connection_ref": "example-edge",
                "public_addresses": ["198.51.100.7"],
                "unread": {"answered_publicly": "the probe was refused"},
            },
        )

        facts = facts_about((), ("198.51.100.7",))

        unread = [f for f in facts if f.state == UNREADABLE]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].label, "answered_publicly")
        self.assertIn("the probe was refused", unread[0].detail)
        self.assertTrue(labeled(facts, "Address"))

    def test_an_old_reading_is_stale(self):
        store(
            PERIMETER,
            {"record": "perimeter", "public_addresses": ["198.51.100.7"]},
            age=timedelta(days=30),
        )

        facts = facts_about((), ("198.51.100.7",))

        self.assertEqual({f.state for f in facts}, {STALE})


class DisagreementTests(TestCase):
    def test_two_sources_with_different_answers_are_both_kept(self):
        first, second = dns_kinds()
        store(first, record_for(first, "www.example.com", "192.0.2.10"))
        store(second, record_for(second, "www.example.com", "10.0.0.5"))

        facts = facts_about(("www.example.com",), ())

        addresses = labeled(facts, "Address")
        self.assertEqual(sorted(f.value for f in addresses), ["10.0.0.5", "192.0.2.10"])


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

    def _facts(self, subjects):
        with projection_scope():
            return [
                facts_about((f"host-{index}", f"n{index}.example.com"), (f"198.51.100.{index}",))
                for index in range(subjects)
            ]

    def test_more_machines_cost_no_more_queries(self):
        self._inventory(10)

        assert_cost_does_not_grow(
            self, sizes=(1, 10), draw=self._facts, rows=lambda answers: sum(1 for facts in answers if facts)
        )

    def test_more_records_cost_no_more_queries(self):
        assert_subject_cost_does_not_grow(
            self, sizes=(2, 40), arrange=self._inventory, draw=lambda _count: self._facts(2)
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
