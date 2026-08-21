"""Machines, and the fact that nothing declares one.

A machine is here because something reported it -- a credential that reaches it,
a container running on it, a service served from it. That membership rule is the
whole design: adding a VPS is registering it somewhere rather than entering it
here, and a machine that stops being any of those things stops being listed.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderConnection, ProviderInventory

from .machines import machine, machine_catalog
from .services import CONTAINER_KIND


def a_connection(ref, provider, endpoint="", reaches=(), reachable=True):
    return ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id="homelab-server",
        provider=provider,
        endpoint=endpoint,
        reaches=list(reaches),
        reachable=reachable,
        probed=True,
        observed_at=timezone.now(),
    )


def containers(*records):
    ProviderInventory.objects.update_or_create(
        kind=CONTAINER_KIND,
        defaults={
            "records": list(records),
            "reachable": True,
            "observed_at": timezone.now(),
        },
    )


class MembershipTests(TestCase):
    def test_a_portainer_environment_is_a_machine(self):
        a_connection(
            "homelab-portainer",
            "portainer",
            endpoint="https://admin.example",
            reaches=["homelab-server", "cloud-edge"],
        )

        self.assertEqual(
            [item.name for item in machine_catalog()],
            ["cloud-edge", "homelab-server"],
        )

    def test_a_zone_a_dns_token_reaches_is_not_a_machine(self):
        """``reaches`` is polymorphic on purpose.

        A Portainer reports the machines it holds and a DNS token reports the
        zones it may edit. Read the same way, four domains appeared on this page
        as though they were servers.
        """

        a_connection(
            "cloudflare-dns",
            "cloudflare_dns",
            endpoint="https://api.example/client/v4",
            reaches=["example.com", "example.net"],
        )

        self.assertEqual(machine_catalog(), ())

    def test_a_connection_that_opens_a_shell_is_itself_a_machine(self):
        """Nothing else names it, and it is still somewhere HQ can log into."""

        a_connection("edge", "ssh", endpoint="198.51.100.7:22")

        found = machine_catalog()
        self.assertEqual([item.name for item in found], ["edge"])
        self.assertEqual(found[0].address, "198.51.100.7:22")

    def test_a_machine_running_something_is_listed_without_a_credential(self):
        """Reported by a container rather than by anything that opens it.

        Said to be unreachable it would read as an outage; what is true is that
        nothing HQ holds gets to it, which the page says instead.
        """

        containers({"name": "probe", "host": "somewhere", "state": "running"})

        found = machine_catalog()
        self.assertEqual([item.name for item in found], ["somewhere"])
        self.assertEqual(found[0].reached_by, ())

    def test_nothing_is_listed_before_anything_has_reported(self):
        self.assertEqual(machine_catalog(), ())


class TieTests(TestCase):
    def setUp(self):
        a_connection(
            "homelab-portainer",
            "portainer",
            endpoint="https://admin.example",
            reaches=["homelab-server"],
        )
        containers(
            {
                "name": "app",
                "host": "homelab-server",
                "state": "running",
                "ports": [8081],
            },
            {"name": "old", "host": "homelab-server", "state": "exited", "ports": []},
        )

    def test_it_counts_what_is_running_rather_than_what_exists(self):
        found = machine(" Homelab-Server ")

        self.assertEqual(len(found.containers), 2)
        self.assertEqual(found.running, 1)

    def test_a_credential_that_answered_makes_the_machine_reachable(self):
        self.assertTrue(machine("homelab-server").reachable)

    def test_a_credential_that_did_not_answer_does_not(self):
        ProviderConnection.objects.update(reachable=False)

        self.assertFalse(machine("homelab-server").reachable)

    def test_an_unreported_name_is_not_a_machine(self):
        self.assertIsNone(machine("nowhere"))


class MachinePageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)
        a_connection(
            "homelab-portainer",
            "portainer",
            endpoint="https://admin.example",
            reaches=["homelab-server"],
        )
        containers(
            {
                "name": "app",
                "host": "homelab-server",
                "image": "nginx:alpine",
                "state": "running",
                "ports": [8081],
            }
        )

    def test_the_board_lists_what_is_on_each(self):
        response = self.client.get(reverse("control_plane:machines"))

        self.assertContains(response, "homelab-server")
        self.assertContains(response, "1 of 1 running")

    def test_the_page_gathers_what_ties_to_one(self):
        response = self.client.get(
            reverse("control_plane:machine", kwargs={"name": "homelab-server"})
        )

        self.assertContains(response, "homelab-portainer")
        self.assertContains(response, "nginx:alpine")
        self.assertContains(response, "8081")

    def test_a_name_nothing_reported_is_not_a_page(self):
        response = self.client.get(
            reverse("control_plane:machine", kwargs={"name": "nowhere"})
        )

        self.assertEqual(response.status_code, 404)

    def test_the_page_never_carries_a_credential(self):
        response = self.client.get(
            reverse("control_plane:machine", kwargs={"name": "homelab-server"})
        )

        self.assertNotContains(response, "API_TOKEN")
        self.assertNotContains(response, "PASSWORD")
