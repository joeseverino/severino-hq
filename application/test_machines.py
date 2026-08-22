"""Machines, and where each one's facts come from.

Observation first: a machine is here because something reported it -- a
credential that reaches it, a container running on it, a service served from it
-- so adding a VPS is registering it somewhere rather than entering it here.

A declaration is the other half, for what nothing can sweep: a printer, an
offline CA, a phone. It carries what the machine is for and the addresses that
reach it, and it puts the machine on the board by itself.
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
        controller_id="a-controller",
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
            "a-portainer",
            "portainer",
            endpoint="https://portainer.example",
            reaches=["a-docker-host", "a-cloud-host"],
        )

        self.assertEqual(
            [item.name for item in machine_catalog()],
            ["a-cloud-host", "a-docker-host"],
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
            "a-portainer",
            "portainer",
            endpoint="https://portainer.example",
            reaches=["a-docker-host"],
        )
        containers(
            {
                "name": "app",
                "host": "a-docker-host",
                "state": "running",
                "ports": [8081],
            },
            {"name": "old", "host": "a-docker-host", "state": "exited", "ports": []},
        )

    def test_it_counts_what_is_running_rather_than_what_exists(self):
        found = machine(" A-Docker-Host ")

        self.assertEqual(len(found.containers), 2)
        self.assertEqual(found.running, 1)

    def test_a_credential_that_answered_makes_the_machine_reachable(self):
        self.assertTrue(machine("a-docker-host").reachable)

    def test_a_credential_that_did_not_answer_does_not(self):
        ProviderConnection.objects.update(reachable=False)

        self.assertFalse(machine("a-docker-host").reachable)

    def test_an_unreported_name_is_not_a_machine(self):
        self.assertIsNone(machine("nowhere"))


class MachinePageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)
        a_connection(
            "a-portainer",
            "portainer",
            endpoint="https://portainer.example",
            reaches=["a-docker-host"],
        )
        containers(
            {
                "name": "app",
                "host": "a-docker-host",
                "image": "nginx:alpine",
                "state": "running",
                "ports": [8081],
            }
        )

    def test_the_board_lists_what_is_on_each(self):
        response = self.client.get(reverse("control_plane:machines"))

        self.assertContains(response, "a-docker-host")
        self.assertContains(response, "1 of 1 running")

    def test_the_page_gathers_what_ties_to_one(self):
        response = self.client.get(
            reverse("control_plane:machine", kwargs={"name": "a-docker-host"})
        )

        self.assertContains(response, "a-portainer")
        self.assertContains(response, "nginx:alpine")
        self.assertContains(response, "8081")

    def test_a_name_nothing_reported_is_not_a_page(self):
        response = self.client.get(
            reverse("control_plane:machine", kwargs={"name": "nowhere"})
        )

        self.assertEqual(response.status_code, 404)

    def test_the_page_never_carries_a_credential(self):
        response = self.client.get(
            reverse("control_plane:machine", kwargs={"name": "a-docker-host"})
        )

        self.assertNotContains(response, "API_TOKEN")
        self.assertNotContains(response, "PASSWORD")


class DeclaredMachineTests(TestCase):
    """A machine HQ was told about shows what it was told.

    The page printed a role that came from a declaration, said nothing declared
    the machine, and left the address blank while the same declaration carried
    two. Three statements about one record, disagreeing.
    """

    def setUp(self):
        from control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="a-laptop",
            kind="machine",
            spec={
                "name": "a-laptop",
                "role": "Primary admin device",
                "addresses": ["10.0.0.5", "100.64.0.5"],
            },
        )

    def machine(self):
        from .machines import machine

        return machine("a-laptop")

    def test_it_appears_without_anything_reaching_it(self):
        self.assertIsNotNone(self.machine())

    def test_it_links_the_declaration_that_describes_it(self):
        self.assertEqual(self.machine().declaration, "a-laptop")

    def test_it_shows_an_address_it_was_told_about(self):
        self.assertEqual(self.machine().address, "10.0.0.5")

    def test_a_machine_nobody_declared_links_nothing(self):
        from control_plane.models import ProviderInventory
        from django.utils import timezone

        ProviderInventory.objects.create(
            kind="portainer.container",
            records=[{"host": "a-docker-host", "name": "web", "ports": [80]}],
            observed_at=timezone.now(),
        )
        from .machines import machine

        found = machine("a-docker-host")
        self.assertIsNotNone(found)
        self.assertEqual(found.declaration, "")


class TailnetPresenceTests(TestCase):
    """Whether a machine is up, said by the network rather than by a service.

    Every other reading HQ has is about something running on a machine, so a box
    that is switched off and a box whose credential expired look identical.
    """

    def sweep(self, *records):
        from django.utils import timezone

        from control_plane.models import ProviderInventory

        ProviderInventory.objects.update_or_create(
            kind="tailscale.device",
            defaults={"records": list(records), "observed_at": timezone.now()},
        )

    def device(self, name, *, online=True, key_expires=""):
        return {"name": name, "online": online, "key_expires": key_expires}

    def test_a_machine_only_the_tailnet_knows_is_still_a_machine(self):
        from .machines import machine

        self.sweep(self.device("a-laptop"))

        self.assertIsNotNone(machine("a-laptop"))

    def test_it_reports_being_on_the_tailnet(self):
        from .machines import machine

        self.sweep(self.device("a-laptop", online=True))

        self.assertTrue(machine("a-laptop").presence.online)

    def test_a_machine_that_is_off_says_so_rather_than_going_missing(self):
        from .machines import machine

        self.sweep(self.device("a-laptop", online=False))

        found = machine("a-laptop")
        self.assertIsNotNone(found)
        self.assertFalse(found.presence.online)

    def test_a_machine_with_no_tailnet_record_has_no_presence(self):
        from control_plane.models import ManagedResource

        from .machines import machine

        ManagedResource.objects.create(
            key="a-printer", kind="machine", spec={"name": "a-printer"}
        )

        self.assertIsNone(machine("a-printer").presence)


class KeyExpiryTests(TestCase):
    """A deadline with no symptom until it passes."""

    def sweep(self, name, days):
        from datetime import timedelta

        from django.utils import timezone

        from control_plane.models import ProviderInventory

        ProviderInventory.objects.update_or_create(
            kind="tailscale.device",
            defaults={
                "records": [
                    {
                        "name": name,
                        "online": True,
                        "key_expires": (
                            timezone.now() + timedelta(days=days)
                        ).isoformat(),
                    }
                ],
                "observed_at": timezone.now(),
            },
        )

    def titles(self):
        from .attention import tailnet

        return [item.title for item in tailnet()]

    def test_an_expiry_inside_the_window_is_raised(self):
        self.sweep("an-edge", 30)

        self.assertEqual(len(self.titles()), 1)

    def test_one_far_away_is_not_raised_at_all(self):
        """A queue that is never empty is one nobody reads."""

        self.sweep("an-edge", 120)

        self.assertEqual(self.titles(), [])

    def test_one_close_enough_is_serious_rather_than_routine(self):
        from .attention import tailnet

        self.sweep("an-edge", 3)

        self.assertEqual([item.status for item in tailnet()], ["serious"])

    def test_expiry_disabled_is_silent_because_there_is_no_date(self):
        from django.utils import timezone

        from control_plane.models import ProviderInventory

        ProviderInventory.objects.update_or_create(
            kind="tailscale.device",
            defaults={
                "records": [{"name": "a-server", "online": True, "key_expires": ""}],
                "observed_at": timezone.now(),
            },
        )

        self.assertEqual(self.titles(), [])


class OneMachineManyNamesTests(TestCase):
    """A tailnet calls a machine whatever its owner typed into it years ago.

    That is rarely the name HQ uses, so without a join the board grows a second
    row for a machine it already had -- with the presence on one row and every
    other fact on the other.
    """

    def setUp(self):
        from django.utils import timezone

        from control_plane.models import ManagedResource, ProviderInventory

        ManagedResource.objects.create(
            key="a-laptop",
            kind="machine",
            spec={
                "name": "a-laptop",
                "role": "Primary admin device",
                "addresses": ["10.0.0.5", "100.64.0.5"],
            },
        )
        ProviderInventory.objects.create(
            kind="tailscale.device",
            records=[
                {"name": "Someone's Laptop", "online": True,
                 "addresses": ["100.64.0.5"]},
                {"name": "a-stranger", "online": True,
                 "addresses": ["100.64.0.99"]},
            ],
            observed_at=timezone.now(),
        )

    def names(self):
        from .machines import machine_catalog

        return [found.name for found in machine_catalog()]

    def test_the_tailnet_name_does_not_become_a_second_machine(self):
        self.assertNotIn("Someone's Laptop", self.names())

    def test_the_name_hq_already_uses_is_the_one_kept(self):
        self.assertIn("a-laptop", self.names())

    def test_presence_follows_the_machine_rather_than_the_name(self):
        from .machines import machine

        self.assertTrue(machine("a-laptop").presence.online)

    def test_a_machine_hq_has_no_address_for_stays_its_own_row(self):
        """Not a missed duplicate: HQ declining to claim two things are one."""

        self.assertIn("a-stranger", self.names())
