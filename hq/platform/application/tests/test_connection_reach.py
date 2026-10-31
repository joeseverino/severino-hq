"""How HQ reaches a connection, from the endpoint, its DNS answers and the machine catalog."""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..connection_reach import connection_reach


def declare(name, *addresses):
    ManagedResource.objects.create(key=name, kind="machine", spec={"name": name, "addresses": list(addresses)})


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
        declare("example-host", "100.64.0.9")
        declare("example-other", "100.64.0.10")
        answer(("hq.example.com", "100.64.0.9"), ("nas.example.com", "100.64.0.10"))

    def test_a_connection_to_the_machine_hq_runs_on_is_reached_locally(self):
        found = connection_reach([("by-address", "100.64.0.9:22"), ("by-name", "example-host:22")])

        for ref in ("by-address", "by-name"):
            with self.subTest(ref=ref):
                self.assertEqual(found[ref].network, "loopback")
                self.assertEqual(found[ref].machine.label, "example-host")

    def test_a_name_hq_reads_an_answer_for_is_reached_at_that_address(self):
        reach = connection_reach([("nas", "https://nas.example.com")])["nas"]

        self.assertEqual((reach.network, reach.address), ("tailnet", "100.64.0.10"))
        self.assertEqual(reach.machine.label, "example-other")

    def test_a_name_no_reading_answers_is_placed_by_the_name_alone(self):
        found = connection_reach(
            [
                ("machine", "example-other:22"),
                ("special-use", "https://printer.home.arpa"),
                ("public", "https://api.example.net"),
            ]
        )

        self.assertEqual((found["machine"].network, found["machine"].machine.label), ("network", "example-other"))
        self.assertEqual((found["special-use"].network, found["special-use"].machine), ("network", None))
        self.assertEqual((found["public"].network, found["public"].machine), ("public", None))
        self.assertEqual({reach.address for reach in found.values()}, {""})

    def test_an_endpoint_without_a_reference_or_a_host_is_left_out(self):
        found = connection_reach([("", "100.64.0.10:22"), ("no-host", "")])

        self.assertEqual(found, {})
