"""Facts about a subject: joined through declared keys, sourced, and never silent."""

from __future__ import annotations

from control_plane.providers import PROVIDERS

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderInventory
from control_plane.observations import OBSERVATIONS, ObservationRecord, ObservationSpec, registry
from control_plane.providers import CONTAINER_KIND

from .facts import OBSERVED, STALE, UNREADABLE, disagreements, facts_about
from .projection import projection_scope

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


def labelled(facts, label):
    return [fact for fact in facts if fact.label == label]


class JoinTests(TestCase):
    def test_a_reading_joins_by_address(self):
        store(
            PERIMETER,
            {"record": "perimeter", "connection_ref": "example-edge",
             "public_addresses": ["198.51.100.7"]},
            {"record": "perimeter", "connection_ref": "other-edge",
             "public_addresses": ["198.51.100.8"]},
        )

        facts = facts_about((), ("198.51.100.7",))

        addresses = labelled(facts, "Address")
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

        self.assertEqual([f.value for f in labelled(facts, "Address")], ["192.0.2.10"])
        self.assertEqual({f.source_kind for f in facts}, {kind})

    def test_a_container_joins_by_the_host_it_runs_on(self):
        store(
            CONTAINER_KIND,
            {"name": "web", "host": "example-host", "host_address": "10.0.0.5",
             "state": "running", "connection_ref": "example-portainer"},
            {"name": "db", "host": "other-host", "state": "running"},
        )

        facts = facts_about(("example-host",), ())

        self.assertEqual([f.value for f in labelled(facts, "Container")], ["web"])
        self.assertEqual([f.value for f in labelled(facts, "Address")], ["10.0.0.5"])

    def test_nothing_joins_a_subject_with_no_keys(self):
        store(PERIMETER, reachable=False, error="refused")

        self.assertEqual(facts_about((), ()), ())


class UnreadableTests(TestCase):
    def test_an_unreachable_kind_is_one_fact_with_its_reason_and_requires(self):
        class Named(ObservationRecord):
            name: str

        spec = ObservationSpec(
            "example.reading", "example", "Example reading", Named, requires=("Zone Read",)
        )
        store("example.reading", reachable=False, error="403 from the provider.")

        with mock.patch("application.facts.OBSERVATIONS", registry((spec,))):
            facts = facts_about(("example-host",), ())

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].state, UNREADABLE)
        self.assertIn("403 from the provider.", facts[0].detail)
        self.assertIn("Zone Read", facts[0].detail)

    def test_an_unreachable_inventory_kind_is_not_silence(self):
        kind, _other = dns_kinds()
        store(kind, reachable=False, error="connection refused")

        facts = facts_about(("www.example.com",), ())

        self.assertEqual([(f.source_kind, f.state) for f in facts], [(kind, UNREADABLE)])
        self.assertIn("connection refused", facts[0].detail)

    def test_a_partial_read_names_the_part_it_could_not_read(self):
        store(
            PERIMETER,
            {"record": "perimeter", "connection_ref": "example-edge",
             "public_addresses": ["198.51.100.7"],
             "unread": {"answered_publicly": "the probe was refused"}},
        )

        facts = facts_about((), ("198.51.100.7",))

        unread = [f for f in facts if f.state == UNREADABLE]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].label, "answered_publicly")
        self.assertIn("the probe was refused", unread[0].detail)
        self.assertTrue(labelled(facts, "Address"))

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

        addresses = labelled(facts, "Address")
        self.assertEqual(sorted(f.value for f in addresses), ["10.0.0.5", "192.0.2.10"])
        differs = disagreements(facts)
        self.assertEqual(set(differs), set(addresses))

    def test_sources_that_agree_do_not_differ(self):
        first, second = dns_kinds()
        store(first, record_for(first, "www.example.com", "192.0.2.10"))
        store(second, record_for(second, "www.example.com", "192.0.2.10"))

        facts = facts_about(("www.example.com",), ())

        self.assertEqual(disagreements(facts), {})


class SeveralValuesTests(TestCase):
    def test_several_addresses_are_not_a_disagreement(self):
        from .facts import Fact, OBSERVED, disagreements

        def fact(source, value):
            return Fact(label="Address", value=value, source_kind=source,
                        source_label=source, connection_ref="", observed_at=None,
                        state=OBSERVED, detail="")

        tailnet = (fact("tailnet", "100.64.0.5"), fact("tailnet", "fd7a::5"))
        containers = (fact("containers", "10.0.0.5"),)
        self.assertEqual(disagreements(tailnet + containers), {})

        single = (fact("a", "1.0"), fact("b", "2.0"))
        self.assertEqual(set(disagreements(single)), set(single))


class QueryCountTests(TestCase):
    """The page reads each kind once, whatever it holds or is asked about."""

    def _inventory(self, count):
        kind, other = dns_kinds()
        store(kind, *(record_for(kind, f"n{i}.example.com", f"10.0.1.{i}") for i in range(count)))
        store(other, *(record_for(other, f"n{i}.example.com", f"10.0.2.{i}") for i in range(count)))
        store(
            PERIMETER,
            *(
                {"record": "perimeter", "connection_ref": f"edge-{i}",
                 "public_addresses": [f"198.51.100.{i}"]}
                for i in range(count)
            ),
        )
        store(
            CONTAINER_KIND,
            *(
                {"name": f"c{i}", "host": f"host-{i}", "host_address": f"10.0.3.{i}"}
                for i in range(count)
            ),
        )

    def _count(self, subjects):
        with CaptureQueriesContext(connection) as captured, projection_scope():
            for index in range(subjects):
                facts_about(
                    (f"host-{index}", f"n{index}.example.com"), (f"198.51.100.{index}",)
                )
        return len(captured)

    def test_more_machines_cost_no_more_queries(self):
        self._inventory(10)

        self.assertEqual(self._count(1), self._count(10))

    def test_more_records_cost_no_more_queries(self):
        self._inventory(2)
        small = self._count(2)
        self._inventory(40)

        self.assertEqual(small, self._count(2))

    def test_the_machine_page_does_not_scale_with_records(self):
        user = get_user_model().objects.create_user("facts-op", password="x" * 20)
        self.client.force_login(user)

        def page(count):
            self._inventory(count)
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(
                    reverse("control_plane:machine", kwargs={"name": "host-1"})
                )
            self.assertEqual(response.status_code, 200)
            return len(captured)

        small = page(3)
        large = page(40)

        # Counts only, never the SQL: this repository is public.
        self.assertLessEqual(large, small, f"the page scales with records ({small} then {large})")


class InZoneTests(TestCase):
    def test_label_boundaries_wildcards_and_spelling(self):
        from control_plane.names import in_zone
        from control_plane.providers import NameContext

        self.assertTrue(in_zone("example.com", "example.com"))
        self.assertTrue(in_zone("*.Example.com.", "example.COM."))
        self.assertTrue(in_zone("a.b.example.com", "example.com"))
        self.assertFalse(in_zone("notexample.com", "example.com"))
        self.assertFalse(in_zone("example.com", ""))
        self.assertEqual(
            NameContext(hostname="a.example.com", public_zones=("example.net", "example.com")).public_zone,
            "example.com",
        )
