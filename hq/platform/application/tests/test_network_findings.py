"""Findings about reach: a consumer that cannot be reached, the tailnet's claims and facts, the perimeter."""

from django.test import TestCase, override_settings
from django.utils import timezone

from ..reach import TAILNET


class UnreachableConsumerTests(TestCase):
    """A name the estate serves that the last reading could not reach.

    The other consumers answered and agree, so nothing here says the
    certificate is wrong. It says one path is shut, and a consumer nobody can
    read keeps whatever it was last given without anything noticing.
    """

    def _findings(self, *names):
        from hq.platform.application.connection_findings import _unreachable_consumer
        from hq.platform.application.findings import _estate
        from hq.platform.application.topology_model import Topology, TopologyNode

        node = TopologyNode(
            id="resource:a-certificate",
            kind="resource",
            label="a-certificate",
            subtitle="TLS certificate",
            kind_key="tls.certificate",
            facts=tuple(("unreachable", name) for name in names),
        )
        return _unreachable_consumer(_estate(Topology(nodes=(node,), edges=())))

    def test_a_consumer_that_answered_nothing_is_named(self):
        (finding,) = self._findings("health.example")

        self.assertEqual(finding.rule, "unreachable-consumer")
        self.assertEqual(finding.severity, "serious")
        self.assertIn("health.example", finding.title)
        self.assertEqual(finding.evidence, (("Did not answer", "health.example"),))
        self.assertTrue(finding.remedies, "nothing could ever clear this")

    def test_every_unread_name_is_evidence(self):
        """The facts share one key, so a dictionary of them would report one.

        A certificate with three unreadable consumers and a finding naming a
        single one is worse than no finding: it reads as the other two being
        fine.
        """

        (finding,) = self._findings("one.example", "two.example", "three.example")

        self.assertEqual(
            finding.evidence,
            (
                ("Did not answer", "one.example"),
                ("Did not answer", "two.example"),
                ("Did not answer", "three.example"),
            ),
        )
        self.assertIn("3", finding.title)

    def test_a_certificate_that_was_read_raises_nothing(self):
        self.assertEqual(self._findings(), ())


class PathRefusedByTheTailnetTests(TestCase):
    """The claim that says which of two problems this is.

    A consumer that is merely down and one the policy refuses look identical
    from a failed reading. Only the second has a change worth offering, and
    offering it for the first would send an operator to widen an access policy
    that was never the problem.
    """

    def _finding(self, *, refused):
        from hq.platform.application.connection_findings import _unreachable_consumer
        from hq.platform.application.findings import _estate
        from hq.platform.application.topology_model import Topology, TopologyNode

        facts = [("unreachable", "health.example")]
        if refused:
            facts.append(("path-denied", "a-controller to an-edge on 443"))
        node = TopologyNode(
            id="resource:a-certificate",
            kind="resource",
            label="a-certificate",
            subtitle="TLS certificate",
            kind_key="tls.certificate",
            facts=tuple(facts),
        )
        (finding,) = _unreachable_consumer(_estate(Topology(nodes=(node,), edges=())))
        return finding

    def test_a_refused_path_offers_the_amendment_with_no_path_of_its_own(self):
        """A remedy names a capability and a target, never a rule to write."""

        finding = self._finding(refused=True)

        (remedy,) = [item for item in finding.remedies if item.capability == "tailnet.reach.allow"]
        self.assertEqual((remedy.target, remedy.effect), ("a-certificate", "infrastructure_change"))
        self.assertIn(("Blocked by tailnet policy", "a-controller to an-edge on 443"), finding.evidence)
        self.assertIn("a-controller to an-edge on 443", finding.explanation)

    def test_a_consumer_merely_down_is_not_sent_at_the_policy(self):
        finding = self._finding(refused=False)

        offered = {remedy.capability for remedy in finding.remedies}
        self.assertNotIn("tailnet.reach.allow", offered)
        self.assertIn("infrastructure.resource.reconcile", offered)


class TailnetClaimTests(TestCase):
    """What the tailnet readings say about its DNS and about HQ's trust."""

    def _findings(self, detect, *facts):
        from hq.platform.application.findings import _estate
        from hq.platform.application.topology_model import Topology, TopologyNode

        node = TopologyNode(
            id="connection:infrastructure.controllers:a-host:a-tailnet",
            kind="connection",
            label="a-tailnet",
            subtitle="tailscale",
            facts=tuple(facts),
        )
        return detect(_estate(Topology(nodes=(node,), edges=())))

    def test_a_resolver_off_the_tailnet_is_claimed(self):
        from hq.platform.application.tailnet_findings import _tailnet_dns_off_tailnet

        (finding,) = self._findings(_tailnet_dns_off_tailnet, ("tailnet-dns-off-tailnet", "192.0.2.53"))

        self.assertEqual(finding.severity, "attention")
        self.assertIn("192.0.2.53", finding.title)
        self.assertEqual(finding.evidence, (("DNS server", "192.0.2.53"),))

    def test_a_resolver_on_the_tailnet_claims_nothing(self):
        from hq.platform.application.tailnet_findings import _tailnet_dns_off_tailnet

        self.assertEqual(
            self._findings(_tailnet_dns_off_tailnet, ("tailnet-address", "100.64.0.53")),
            (),
        )

    @override_settings(SEVERINO_TRUSTED_NETWORKS=["127.0.0.0/8", str(TAILNET[0])])
    def test_trusting_the_whole_range_is_reported_with_what_the_tailnet_uses(self):
        from hq.platform.application.trusted_networks import wider_than_tailnet

        (finding,) = self._findings(
            wider_than_tailnet,
            ("tailnet-address", "100.64.0.1"),
            ("tailnet-address", "100.64.0.2"),
            ("tailnet-route", "192.0.2.0/24"),
        )

        self.assertEqual(finding.severity, "neutral")
        self.assertEqual(
            finding.evidence,
            (
                ("Trusted", str(TAILNET[0])),
                ("Device address", "100.64.0.1"),
                ("Device address", "100.64.0.2"),
                ("Subnet route", "192.0.2.0/24"),
            ),
        )
        self.assertEqual(finding.remedies, ())

    @override_settings(SEVERINO_TRUSTED_NETWORKS=["127.0.0.0/8", "::1/128", *map(str, TAILNET)])
    def test_both_families_are_narrowed_to_device_hosts_and_loopback_is_kept(self):
        from hq.platform.application.trusted_networks import wider_than_tailnet

        (finding,) = self._findings(
            wider_than_tailnet,
            ("tailnet-address", "fd7a:115c:a1e0::2"),
            ("tailnet-address", "100.64.0.1"),
            ("tailnet-address", "fd7a:115c:a1e0::1"),
            ("tailnet-route", "192.0.2.0/24"),
        )

        self.assertIn(f"{TAILNET[0]}, {TAILNET[1]}", finding.title)
        self.assertEqual(
            finding.steps[0].command,
            "SEVERINO_TRUSTED_NETWORKS=127.0.0.0/8,::1/128,100.64.0.1/32,"
            "fd7a:115c:a1e0::1/128,fd7a:115c:a1e0::2/128,192.0.2.0/24",
        )
        self.assertNotIn(str(TAILNET[1]), finding.steps[0].command)

    def test_only_a_wide_range_a_device_address_falls_in_is_narrowed(self):
        from hq.platform.application.trusted_networks import wider_than_tailnet

        cases = (
            (["::1/128", str(TAILNET[1])], ("100.64.0.1", "fd7a:115c:a1e0::1"), "::1/128,fd7a:115c:a1e0::1/128"),
            ([*map(str, TAILNET)], ("100.64.0.1",), f"{TAILNET[1]},100.64.0.1/32"),
        )
        for trusted, addresses, narrowed in cases:
            with self.subTest(trusted=trusted), override_settings(SEVERINO_TRUSTED_NETWORKS=trusted):
                (finding,) = self._findings(
                    wider_than_tailnet, *(("tailnet-address", address) for address in addresses)
                )

                self.assertEqual(finding.steps[0].command, f"SEVERINO_TRUSTED_NETWORKS={narrowed}")

    @override_settings(SEVERINO_TRUSTED_NETWORKS=["not a network", str(TAILNET[0])])
    def test_an_invalid_trusted_entry_is_skipped_not_raised(self):
        from hq.platform.application.trusted_networks import wider_than_tailnet

        (finding,) = self._findings(wider_than_tailnet, ("tailnet-address", "100.64.0.1"))

        self.assertTrue(finding.steps[0].command.endswith("100.64.0.1/32"))

    def test_trust_already_narrowed_claims_nothing(self):
        from hq.platform.application.trusted_networks import wider_than_tailnet

        for trusted, address in (
            (["127.0.0.0/8", "100.64.0.1/32"], "100.64.0.1"),
            (["::1/128", "fd7a:115c:a1e0::1/128"], "fd7a:115c:a1e0::1"),
        ):
            with self.subTest(trusted=trusted), override_settings(SEVERINO_TRUSTED_NETWORKS=trusted):
                self.assertEqual(self._findings(wider_than_tailnet, ("tailnet-address", address)), ())

    @override_settings(SEVERINO_TRUSTED_NETWORKS=[str(TAILNET[0])])
    def test_without_a_device_reading_nothing_is_claimed(self):
        from hq.platform.application.trusted_networks import wider_than_tailnet

        self.assertEqual(self._findings(wider_than_tailnet), ())


class TailnetFactTests(TestCase):
    """The facts the tailnet rules read, derived from the stored readings."""

    def store(self, kind, records):
        from hq.domains.control_plane.models import ProviderInventory

        ProviderInventory.objects.create(kind=kind, records=records, observed_at=timezone.now())

    def facts(self):
        from hq.platform.application.topology_facts import _tailnet_facts

        return _tailnet_facts()

    def test_a_nameserver_that_is_no_device_address_is_off_the_tailnet(self):
        self.store(
            "tailscale.device",
            [
                {
                    "name": "example-host",
                    "addresses": ["100.64.0.53", "fd7a:115c:a1e0::35"],
                    "enabled_routes": ["192.0.2.0/24", "0.0.0.0/0", "::/0"],
                }
            ],
        )
        self.store(
            "tailscale.dns",
            [{"record": "dns", "nameservers": ["100.64.0.53", "192.0.2.53"]}],
        )

        facts = self.facts()

        self.assertIn(("tailnet-dns-off-tailnet", "192.0.2.53"), facts)
        self.assertNotIn(("tailnet-dns-off-tailnet", "100.64.0.53"), facts)
        self.assertIn(("tailnet-address", "100.64.0.53"), facts)
        self.assertIn(("tailnet-address", "fd7a:115c:a1e0::35"), facts)
        self.assertIn(("tailnet-route", "192.0.2.0/24"), facts)
        self.assertNotIn(("tailnet-route", "0.0.0.0/0"), facts)
        self.assertNotIn(("tailnet-route", "::/0"), facts)

    def test_without_a_device_reading_no_resolver_is_judged(self):
        self.store("tailscale.dns", [{"record": "dns", "nameservers": ["192.0.2.53"]}])

        self.assertEqual(self.facts(), ())


class PerimeterClaimTests(TestCase):
    """The two things an edge relies on to stay shut, claimed separately."""

    def _findings(self, detect, *facts):
        from hq.platform.application.findings import _estate
        from hq.platform.application.topology_model import Topology, TopologyNode

        node = TopologyNode(
            id="connection:infrastructure.controllers:a-host:an-edge",
            kind="connection",
            label="an-edge",
            subtitle="ssh",
            facts=tuple(facts),
        )
        return detect(_estate(Topology(nodes=(node,), edges=())))

    def test_a_port_answering_publicly_is_serious(self):
        from hq.platform.application.perimeter_findings import _perimeter_open

        (finding,) = self._findings(
            _perimeter_open,
            ("answers-publicly", "9001"),
            ("answers-publicly", "443"),
        )

        self.assertEqual(finding.severity, "serious")
        self.assertIn("9001", finding.title)
        self.assertEqual(len(finding.evidence), 2)

    def test_a_shut_perimeter_claims_nothing(self):
        from hq.platform.application.perimeter_findings import _perimeter_open

        self.assertEqual(self._findings(_perimeter_open, ("Controller", "a-host")), ())

    def test_a_stopped_firewall_is_claimed_on_its_own(self):
        """Separate from the ports: a control that stopped, not a breach."""

        from hq.platform.application.perimeter_findings import _firewall_stopped

        (finding,) = self._findings(_firewall_stopped, ("firewall-unit", "inactive"))

        self.assertEqual(finding.severity, "serious")
        self.assertEqual(finding.evidence, (("Firewall", "Stopped"),))

    def test_a_running_firewall_claims_nothing(self):
        """Only a state worth acting on reaches the topology at all."""

        from hq.platform.application.perimeter_findings import _firewall_stopped

        self.assertEqual(self._findings(_firewall_stopped), ())
