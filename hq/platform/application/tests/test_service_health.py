"""A service's state says what was checked, and what answers behind its proxy."""

from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..projection import projection_scope
from ..services import find_service


def in_place(key: str, kind: str, spec: dict) -> ManagedResource:
    """A record HQ set and the last reading found as set."""

    return ManagedResource.objects.create(
        key=key,
        kind=kind,
        spec=spec,
        generation=1,
        observed_generation=1,
        last_observed_at=timezone.now(),
        conditions=[{"type": "Ready", "status": True, "reason": "Observed", "message": ""}],
    )


def containers(*records: dict) -> None:
    ProviderInventory.objects.update_or_create(
        kind="portainer.container",
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now(),
            "controller_id": "example-controller",
        },
    )


# 198.51.100.0/24 stands for an address in use; 192.0.2.0/24 stays parked.
IN_USE = mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))


@IN_USE
class ServiceHealthTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        ManagedResource.objects.create(
            key="lab-1", kind="machine", spec={"name": "lab-1", "addresses": ["198.51.100.10"]}
        )
        in_place("app-dns", "adguard.rewrite", {"domain": "app.example.com", "answer": "198.51.100.10"})

    def proxy(self, port: int = 8080) -> None:
        in_place(
            "example-wildcard",
            "tls.certificate",
            {"certificate_name": "example", "domains": ["*.example.com"]},
        )
        in_place(
            "app-proxy",
            "npm.proxy_host",
            {
                "domain_names": ["app.example.com"],
                "forward_scheme": "http",
                "forward_host": "198.51.100.10",
                "forward_port": port,
            },
        )

    def health(self):
        with projection_scope():
            return find_service("app.example.com").health

    def test_a_name_with_only_a_dns_record_is_not_called_healthy(self):
        health = self.health()

        self.assertEqual((health.state, health.label), ("unknown", "DNS record only"))

    def test_records_in_place_say_what_was_checked(self):
        self.proxy()

        health = self.health()

        self.assertEqual(health.label, "Set up")
        self.assertIn("DNS record and proxy are in place", health.detail)
        self.assertIn("HQ does not read what runs there", health.detail)

    def test_a_proxy_forwarding_to_a_port_nothing_publishes_says_so(self):
        self.proxy(8080)
        containers({"host": "lab-1", "name": "other", "ports": [9000], "state": "running"})

        health = self.health()

        self.assertEqual((health.state, health.label), ("attention", "Nothing on that port"))
        self.assertIn("forwards to 198.51.100.10:8080", health.detail)
        self.assertIn("no container on lab-1 publishes that port", health.detail)

    def test_a_proxy_forwarding_to_a_container_names_it(self):
        self.proxy(8080)
        containers({"host": "lab-1", "name": "app", "ports": [8080], "state": "running"})

        health = self.health()

        self.assertEqual((health.state, health.label), ("good", "Set up"))
        self.assertIn("forward to app on lab-1", health.detail)

    def test_no_state_reads_as_healthy(self):
        self.proxy(8080)
        containers({"host": "lab-1", "name": "app", "ports": [8080], "state": "running"})

        self.assertNotIn("Healthy", (self.health().label, self.health().detail))
