"""Findings about reach: a consumer that cannot be reached, the tailnet's claims and facts, the perimeter."""

from django.test import TestCase
from django.utils import timezone


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
