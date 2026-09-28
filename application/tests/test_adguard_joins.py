"""AdGuard readings joined into the graph: devices, services and findings."""

from __future__ import annotations


from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from control_plane.observations import OBSERVATIONS
from control_plane.observations.adguard import CLIENT_KIND, DNS_KIND, QUERY_KIND

from ..facts import Subject, facts_about, inventory_about
from ..findings import derive_findings
from ..relationships import relationships_for
from ..security import Capability, Principal
from ..tailnet import TAILNET_KIND
from ..topology import derive_topology

READ = Principal("reader", "test", frozenset({Capability.READ}))
CONTROLLER = "example-controller"
ADGUARD = "example-adguard"


def store(kind, *records):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": True,
            "controller_id": CONTROLLER,
            "observed_at": timezone.now(),
        },
    )


def connection(ref, provider):
    ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id=CONTROLLER,
        provider=provider,
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


def machine(name, address):
    ManagedResource.objects.create(
        key=name, kind="machine", spec={"name": name, "addresses": [address]}
    )


def summary(domain, queries, **extra):
    return {"connection_ref": ADGUARD, "domain": domain, "queries": queries,
            "client_count": 1 if queries else 0, "window_hours": 24.0, **extra}


class AdGuardJoinTests(TestCase):
    def setUp(self):
        connection(ADGUARD, "adguard")
        machine("lab-1", "100.64.0.10")
        machine("laptop-1", "100.64.0.2")
        for name in ("app", "idle"):
            ManagedResource.objects.create(
                key=f"{name}-rewrite", kind="adguard.rewrite",
                spec={"domain": f"{name}.example.com", "answer": "100.64.0.10"},
            )
        store("adguard.rewrite",
              {"connection_ref": ADGUARD, "domain": "app.example.com", "answer": "100.64.0.10"},
              {"connection_ref": ADGUARD, "domain": "idle.example.com", "answer": "100.64.0.10"})
        store(CLIENT_KIND, {"connection_ref": ADGUARD, "name": "laptop", "source": "persistent",
                            "ids": ["100.64.0.2"], "addresses": ["100.64.0.2"]})
        store(QUERY_KIND,
              summary("app.example.com", 12,
                      clients=[{"address": "100.64.0.2", "name": "laptop", "queries": 12}]),
              summary("idle.example.com", 0))
        store(DNS_KIND, {"connection_ref": ADGUARD, "version": "v0.107.0",
                         "protection_enabled": True, "filtering_enabled": False,
                         "upstreams": [{"host": "198.51.100.53", "transport": "dns"},
                                       {"host": "dns.example.test", "transport": "tls"}]})

    def test_each_rewrite_joins_the_machine_its_answer_names(self):
        joined = inventory_about("adguard.rewrite", Subject.of(addresses=("100.64.0.10",)))

        self.assertEqual({record["domain"] for _s, record in joined},
                         {"app.example.com", "idle.example.com"})
        self.assertIn("adguard.rewrite",
                      {fact.source_kind for fact in facts_about(("lab-1",), ("100.64.0.10",))})

    def test_a_machine_shows_its_dns_client_identity(self):
        found = relationships_for("machine:laptop-1", principal=READ)

        relation = OBSERVATIONS[CLIENT_KIND].relation_to(by_address=True)
        self.assertEqual(found.labels(relation), ("laptop",))
        (item,) = found.group(relation).items
        self.assertEqual(item.source.label, ADGUARD)

    def test_a_declared_tailnet_device_shows_its_dns_client_identity(self):
        store(TAILNET_KIND, {"name": "laptop-1", "addresses": ["100.64.0.2"], "online": True})
        ManagedResource.objects.create(key="laptop-device", kind=TAILNET_KIND,
                                       spec={"name": "laptop-1"})

        found = relationships_for("resource:laptop-device", principal=READ)

        self.assertIn(OBSERVATIONS[CLIENT_KIND].relation_to(by_address=True), found.phrases())

    def test_a_service_shows_that_it_is_used_and_by_whom(self):
        found = relationships_for("service:app.example.com", principal=READ)

        self.assertEqual(found.labels(OBSERVATIONS[QUERY_KIND].relation),
                         ("12 lookups from 1 device in 24 hours: laptop",))

    def test_the_query_aggregate_never_joins_a_device(self):
        found = relationships_for("machine:laptop-1", principal=READ)

        self.assertNotIn(OBSERVATIONS[QUERY_KIND].relation, found.phrases())

    def test_the_service_page_renders_the_usage(self):
        user = get_user_model().objects.create_user("dns-op", password="x" * 20)
        self.client.force_login(user)

        page = self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "app.example.com"})
        )

        self.assertContains(page, "12 lookups from 1 device in 24 hours: laptop")

    def test_posture_and_usage_raise_their_findings(self):
        found = derive_findings(derive_topology(principal=READ), principal=READ)
        by_rule = {finding.rule: finding for finding in found}

        self.assertNotIn("dns-protection-off", by_rule)
        self.assertEqual(by_rule["dns-filtering-off"].severity, "attention")
        self.assertEqual(by_rule["dns-plain-upstream"].evidence,
                         (("Plain upstream", "198.51.100.53"),))
        unused = by_rule["dns-name-unused"]
        self.assertEqual(unused.subject, "service:idle.example.com")
        self.assertEqual(unused.title, "No device looked up idle.example.com")
        self.assertNotIn("app.example.com",
                         {f.title for f in found if f.rule == "dns-name-unused"})

    def test_a_healthy_resolver_raises_nothing(self):
        store(DNS_KIND, {"connection_ref": ADGUARD, "protection_enabled": True,
                         "filtering_enabled": True,
                         "upstreams": [{"host": "dns.example.test", "transport": "tls"},
                                       {"host": "10.0.0.53", "transport": "dns"}]})
        store(QUERY_KIND, summary("app.example.com", 3))

        rules = {f.rule for f in derive_findings(derive_topology(principal=READ), principal=READ)}

        self.assertFalse(rules & {"dns-protection-off", "dns-filtering-off",
                                  "dns-plain-upstream", "dns-name-unused"})

    def test_a_reading_of_another_connection_says_nothing_about_this_one(self):
        connection("other-adguard", "adguard")
        topology = derive_topology(principal=READ)
        other = next(node for node in topology.nodes
                     if node.kind == "connection" and node.connection_ref == "other-adguard")

        self.assertEqual([key for key, _v in other.facts if key.startswith("dns-")], [])
