"""How HQ reaches a connection, from the endpoint, its DNS answers and the machine catalog."""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from ..connection_reach import connection_reach
from .test_hq_self import machine_at


def answer(*pairs):
    ProviderInventory.objects.create(
        kind="adguard.rewrite",
        records=[{"domain": domain, "answer": address} for domain, address in pairs],
        observed_at=timezone.now(),
    )


# The addresses of the machine the tests run on, which no example address is.
OWN = mock.patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset({"127.0.0.1", "::1"}))


@override_settings(SEVERINO_SITE_HOST="hq.example.com", ALLOWED_HOSTS=["hq.example.com", "testserver"])
class ConnectionReachTests(TestCase):
    def setUp(self):
        self.enterContext(OWN)
        machine_at("example-host", "100.64.0.9")
        machine_at("example-other", "100.64.0.10")
        answer(("hq.example.com", "100.64.0.9"), ("nas.example.com", "100.64.0.10"))

    def test_a_connection_to_the_machine_hq_runs_on_is_reached_locally(self):
        found = connection_reach([("by-address", "100.64.0.9:22"), ("by-name", "example-host:22")])

        for ref in ("by-address", "by-name"):
            with self.subTest(ref=ref):
                self.assertEqual(found[ref].network, "loopback")
                self.assertEqual(found[ref].machine.label, "example-host")
