"""Where two connections disagree, each a finding with the fix HQ can offer."""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderInventory

from ..findings import derive_findings
from ..projection import projection_scope
from ..tailnet import unworn_tags
from .test_paths import CLOUDFLARE, PUBLIC_RANGE, estate, record, store
from ..topology import derive_topology
from ..security import Capability, Principal

OPERATOR = Principal(
    "operator", "test", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})
)


def raised(rule):
    with projection_scope():
        return derive_findings(derive_topology(principal=OPERATOR), principal=OPERATOR, rule=rule)


@PUBLIC_RANGE
class ContradictionTests(TestCase):
    def setUp(self):
        estate()

    def test_a_consistent_estate_raises_none(self):
        for rule in (
            "public-name-served-by-nothing", "route-to-stopped-container", "gate-guards-nothing",
            "split-horizon-disagrees", "published-port-unfronted",
        ):
            with self.subTest(rule=rule):
                self.assertEqual(raised(rule), ())

    def test_a_route_to_a_container_that_is_not_running(self):
        store(
            "portainer.container",
            {"host": "lab-1", "name": "app", "ports": [8000], "state": "running"},
            {"host": "edge-1", "name": "shop", "ports": [8080], "state": "exited"},
        )

        (finding,) = raised("route-to-stopped-container")

        self.assertEqual(finding.subject, "service:shop.example.com")
        self.assertIn("shop on edge-1, which is exited", finding.title)
        self.assertEqual(finding.severity, "serious")
        self.assertEqual(finding.steps[0].command, "docker start shop")

    def test_a_gate_on_a_name_no_record_answers(self):
        store("cloudflare.access_app", {"connection_ref": CLOUDFLARE, "id": "a1", "name": "Old admin",
                                        "domain": "gone.example.com", "type": "self_hosted"})

        (finding,) = raised("gate-guards-nothing")

        self.assertIn("Old admin guards gone.example.com", finding.title)

    def test_inside_and_outside_reaching_different_machines(self):
        store(
            "cloudflare.dns_record",
            record("shop.example.com", "A", "198.51.100.20"),
            record("db.example.com", "A", "198.51.100.20", proxied=False),
        )
        store(
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
            {"domain": "db.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
        )
        ManagedResource.objects.create(
            key="example-db-rewrite", kind="adguard.rewrite",
            spec={"domain": "db.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
        )

        (finding,) = raised("split-horizon-disagrees")

        self.assertIn("db.example.com lives on edge-1 publicly and lab-1 inside", finding.title)
        self.assertEqual([remedy.target for remedy in finding.remedies], ["example-db-rewrite"])
        self.assertIn("?target=example-db-rewrite", finding.remedies[0].url)

    def test_a_proxy_that_carries_both_answers_to_one_machine_agrees(self):
        store(
            "cloudflare.dns_record",
            record("shop.example.com", "A", "198.51.100.20"),
            record("app.example.com", "A", "198.51.100.20", proxied=False),
        )

        self.assertEqual(raised("split-horizon-disagrees"), ())

    def test_a_public_name_its_machine_serves_nothing_for(self):
        store(
            "cloudflare.dns_record",
            record("shop.example.com", "A", "198.51.100.20"),
            record("old.example.com", "A", "198.51.100.20", proxied=False),
        )

        titles = [finding.title for finding in raised("public-name-served-by-nothing")]

        self.assertEqual(titles, ["old.example.com points at edge-1, which serves nothing for it"])

    def test_a_port_answering_the_internet_with_nothing_in_front(self):
        store(
            "portainer.container",
            {"host": "lab-1", "name": "app", "ports": [8000], "state": "running"},
            {"host": "edge-1", "name": "shop", "ports": [8080], "state": "running"},
            {"host": "edge-1", "name": "stray", "ports": [9000], "state": "running"},
        )
        store("host.perimeter", {"record": "perimeter", "connection_ref": "example-ssh",
                                 "public_addresses": ["198.51.100.20"], "ports_checked": [8080, 9000],
                                 "answered_publicly": [8080, 9000]})

        (finding,) = raised("published-port-unfronted")

        # shop answers too, but a route leads to it: only stray has nothing in front.
        self.assertIn("stray on edge-1 answers the internet on port 9000", finding.title)


class ServedCertificateTests(TestCase):
    def test_a_host_serving_another_certificate_offers_the_reinstall(self):
        from .test_paths import declare_certificate

        declare_certificate(matches=False)

        (finding,) = raised("served-certificate-not-held")

        self.assertEqual(finding.subject, "resource:example-wildcard")
        self.assertIn("app.example.com serves a certificate other than example-wildcard", finding.title)
        # The node's own reconcile action supplies the link where it is enabled.
        self.assertEqual(
            [(remedy.capability, remedy.target) for remedy in finding.remedies],
            [("infrastructure.reconcile", "example-wildcard")],
        )

    def test_a_host_serving_the_held_certificate_is_quiet(self):
        from .test_paths import declare_certificate

        declare_certificate(matches=True)

        self.assertEqual(raised("served-certificate-not-held"), ())


class UnwornTagTests(TestCase):
    def test_a_tag_no_device_wears_is_named_and_ports_are_not_part_of_it(self):
        ProviderInventory.objects.create(
            kind="tailscale.device", observed_at=timezone.now(),
            records=[{"name": "example-device", "tags": ["tag:web"]}],
        )

        self.assertEqual(unworn_tags({"tag:web:443", "tag:db:5432", "group:admins"}), ("tag:db",))

    def test_nothing_is_said_before_devices_are_read(self):
        self.assertEqual(unworn_tags({"tag:db"}), ())
