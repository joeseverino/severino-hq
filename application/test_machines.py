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

from control_plane.models import (
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
)

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

    def test_it_projects_telemetry_from_the_same_declaration(self):
        from control_plane.models import ManagedResource
        from django.utils import timezone

        observed = timezone.now()
        declared = ManagedResource.objects.get(key="a-laptop")
        declared.status = {
            "telemetry": {
                "status": "good",
                "metrics": [{"label": "CPU", "value": "8%"}],
            }
        }
        declared.last_observed_at = observed
        declared.save(update_fields=("status", "last_observed_at", "updated_at"))

        found = self.machine()

        self.assertEqual(found.telemetry["metrics"][0]["value"], "8%")
        self.assertEqual(found.telemetry_observed_at, observed)

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

    def test_a_key_that_has_shaken_hands_is_a_peer_and_one_that_has_not_is_not(self):
        """The reading comes from the daemon on the machine HQ runs on, so a
        device appearing at all is in HQ's network map. A key in a map is a
        machine HQ could reach; a handshake is one it is reaching."""

        from .machines import tailnet_presence

        self.sweep(
            {
                **self.device("a-peer"),
                "public_key": "test-key-a",
                "last_handshake": "2026-08-25T22:18:24Z",
                "direct_endpoint": "198.51.100.4:41641",
                "relay": "ord",
            },
            {**self.device("never-spoken"), "public_key": "test-key-b"},
        )

        found = tailnet_presence()

        self.assertTrue(found["a-peer"].peered)
        self.assertEqual(found["a-peer"].peer_path, "direct")
        # In the map, never spoken to.
        self.assertFalse(found["never-spoken"].peered)
        self.assertEqual(found["never-spoken"].peer_path, "")

    def test_a_relayed_peer_says_so_rather_than_reading_as_direct(self):
        """Still end to end encrypted, still slower, and worth knowing which."""

        from .machines import tailnet_presence

        self.sweep(
            {
                **self.device("a-relayed-peer"),
                "public_key": "test-key-a",
                "last_handshake": "2026-08-25T22:18:24Z",
                "direct_endpoint": "",
                "relay": "ord",
            }
        )

        self.assertEqual(tailnet_presence()["a-relayed-peer"].peer_path, "relayed")

    def test_a_route_offered_and_never_approved_is_named_as_unapproved(self):
        """The silent failure. `--advertise-routes` succeeds, the machine
        reports the route for as long as it runs, and the coordination server
        hands it to nobody -- so a subnet route can be configured, documented,
        believed, and dead, with every side of it reporting success."""

        from .machines import tailnet_presence

        self.sweep(
            {
                **self.device("a-router"),
                "advertised_routes": ["0.0.0.0/0", "198.51.100.0/24", "::/0"],
                "enabled_routes": ["198.51.100.0/24"],
                "offers_exit_node": True,
                "exit_node_approved": False,
            }
        )

        presence = tailnet_presence()["a-router"]

        self.assertEqual(presence.unapproved_routes, ("0.0.0.0/0", "::/0"))
        self.assertTrue(presence.offers_exit_node)
        self.assertFalse(presence.exit_node_approved)

    def test_a_machine_advertising_nothing_raises_nothing(self):
        """A queue entry per machine that routes nothing would be every machine."""

        from .machines import tailnet_presence

        self.sweep(self.device("a-laptop"))

        self.assertEqual(tailnet_presence()["a-laptop"].unapproved_routes, ())

    def test_every_advertised_route_approved_is_silent(self):
        from .machines import tailnet_presence

        self.sweep(
            {
                **self.device("a-router"),
                "advertised_routes": ["198.51.100.0/24"],
                "enabled_routes": ["198.51.100.0/24"],
            }
        )

        self.assertEqual(tailnet_presence()["a-router"].unapproved_routes, ())

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
                {
                    "name": "Someone's Laptop",
                    "online": True,
                    "addresses": ["100.64.0.5"],
                },
                {"name": "a-stranger", "online": True, "addresses": ["100.64.0.99"]},
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


class WhoeverSweptTests(TestCase):
    """The name a sweep files containers under is not always a machine's name.

    Portainer calls its own environment "local", and a controller filling that
    in has nothing to offer but its own hostname. Run the sweep from a laptop
    and every container on the Docker host is reported as running on the
    laptop -- a machine that has never run any of them.
    """

    def setUp(self):
        from control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="a-docker-host",
            kind="machine",
            spec={
                "name": "a-docker-host",
                "role": "Docker host",
                "addresses": ["10.0.0.9"],
            },
        )
        containers(
            {
                "name": "a-service",
                "host": "a-laptop-that-swept",
                "host_address": "10.0.0.9",
                "state": "running",
                "connection_ref": "a-portainer",
            }
        )

    def test_the_containers_are_on_the_machine_running_them(self):
        found = machine("a-docker-host")

        self.assertEqual([item.name for item in found.containers], ["a-service"])

    def test_the_machine_that_swept_does_not_become_a_second_row(self):
        self.assertNotIn(
            "a-laptop-that-swept", [found.name for found in machine_catalog()]
        )

    def test_the_declared_name_is_the_one_kept(self):
        self.assertIn("a-docker-host", [found.name for found in machine_catalog()])

    def test_the_name_the_sweep_used_is_kept_as_an_alias(self):
        """Discarding it would leave nothing explaining the fold."""

        self.assertIn("a-laptop-that-swept", machine("a-docker-host").aliases)


class ReadingThisOnTheMachineTests(TestCase):
    """The page describing a machine, opened on that machine.

    HQ judged the caller's address for the network gate before any view ran,
    and every machine carries the addresses it answers at. So the page could
    always have known, and instead said "this machine" in the third person
    while somebody looked at their own laptop.
    """

    def setUp(self):
        from control_plane.models import ManagedResource

        self.user = get_user_model().objects.create_user(
            username="an-operator", password="not-used-here"
        )
        self.client.force_login(self.user)
        ManagedResource.objects.create(
            key="a-laptop",
            kind="machine",
            spec={"name": "a-laptop", "role": "", "addresses": ["100.64.0.5"]},
        )

    def page(self, address):
        return self.client.get(
            reverse("control_plane:machine", kwargs={"name": "a-laptop"}),
            REMOTE_ADDR=address,
        )

    def test_the_machine_you_are_reading_it_on_says_so(self):
        self.assertContains(self.page("100.64.0.5"), "this device")

    def test_another_machine_does_not(self):
        self.assertNotContains(self.page("100.64.0.9"), "this device")

    def test_knowing_costs_no_query(self):
        """It is arithmetic on one address, and the header is on every page."""

        from core.network import client_ip
        from django.test import RequestFactory

        request = RequestFactory().get("/", REMOTE_ADDR="100.64.0.5")
        with self.assertNumQueries(0):
            client_ip(request)


class ObservedAddressAnnotationTests(TestCase):
    """A field that records two unrelated things should say which is which.

    Half a machine's addresses are the only record there is -- nothing in the
    estate reports the printer on the LAN. The rest repeat what the tailnet
    says on every sweep, and are also the key that ties HQ's name for a machine
    to the tailnet's, which calls the same laptop something else. Locking the
    field breaks the printer; leaving it silent invites somebody to correct HQ
    about a value HQ is watching.
    """

    def setUp(self):
        ManagedResource.objects.create(
            key="a-box",
            kind="machine",
            spec={
                "name": "a-box",
                "addresses": ["192.0.2.50", "100.101.102.103"],
            },
        )
        ProviderInventory.objects.create(
            kind="tailscale.device",
            observed_at=timezone.now(),
            records=[
                {
                    "name": "A Box",
                    "addresses": ["100.101.102.103"],
                    "os": "linux",
                }
            ],
        )

    def _notes(self):
        from application.provider_choices import machine_address_notes

        return machine_address_notes()["addresses"]

    def test_an_address_the_tailnet_reports_is_marked_as_seen(self):
        self.assertIn("100.101.102.103", self._notes())

    def test_an_address_nothing_reports_is_left_unmarked(self):
        """It is not unverified. It is the only place that address exists."""

        self.assertNotIn("192.0.2.50", self._notes())

    def test_the_form_carries_the_marks_onto_the_field(self):
        from application.provider_forms import spec_form_class

        form = spec_form_class("machine")()

        self.assertEqual(
            form.fields["addresses"].widget.notes.get("100.101.102.103"),
            "seen on the tailnet",
        )

    def test_a_machine_still_declares_an_address_nothing_can_see(self):
        """The printer case, which is why this is annotated and not locked."""

        from application.provider_forms import spec_form_class

        form = spec_form_class("machine")(
            data={"name": "laserjet", "addresses": ["192.0.2.137"]}
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.spec["addresses"], ["192.0.2.137"])

    def test_an_observed_address_is_not_offered_for_removal(self):
        """HQ holds it whether or not this field does.

        Offering to remove it was offering to delete a fact -- and until the
        tailnet's addresses reached the index, removing one silently broke the
        resolution it looked redundant to.
        """

        from application.provider_forms import spec_form_class

        form = spec_form_class("machine")(
            initial={"name": "a-box", "addresses": ["192.0.2.50", "100.101.102.103"]}
        )
        rendered = str(form["addresses"])

        self.assertIn('value="100.101.102.103"', rendered)
        self.assertIn('aria-label="Remove 192.0.2.50"', rendered)
        self.assertNotIn('aria-label="Remove 100.101.102.103"', rendered)

    def test_a_save_keeps_the_observed_address_it_did_not_ask_about(self):
        """Rendered read-only, it still has to come back on submit."""

        from application.provider_forms import spec_form_class

        form = spec_form_class("machine")(
            data={
                "name": "a-box",
                "addresses": ["192.0.2.50", "100.101.102.103"],
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("100.101.102.103", form.spec["addresses"])

    def test_what_hq_found_comes_before_what_only_this_field_records(self):
        """The observed address is where the machine actually answers.

        A read-only row between two inputs also pushed the blank row for adding
        one away from the rest of them.
        """

        from application.provider_forms import spec_form_class

        form = spec_form_class("machine")(
            initial={
                "name": "a-box",
                "addresses": ["192.0.2.50", "100.101.102.103"],
            }
        )
        rendered = str(form["addresses"])

        self.assertLess(rendered.index("100.101.102.103"), rendered.index("192.0.2.50"))


class IdentifierIsFixedTests(TestCase):
    """The identifier is filing, and a form should not ask about filing.

    It was an input near the bottom of the form, behind the Options disclosure,
    directly beneath a field called "Name". So the resource appeared to have two
    names, the fixed one looked like the optional one, and reading it meant
    opening a drawer.
    """

    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="a-box", kind="machine", spec={"name": "a-box", "addresses": []}
        )
        user = get_user_model().objects.create_user(
            username="op", password="pw", is_staff=True, is_superuser=True
        )
        self.client.force_login(user)

    def test_the_form_does_not_ask_for_it(self):
        from application.provider_forms import ResourceIdentityForm

        self.assertNotIn("key", ResourceIdentityForm().fields)

    def test_it_is_shown_at_the_top_instead(self):
        response = self.client.get(
            reverse("control_plane:edit", kwargs={"key": "a-box"})
        )

        self.assertContains(response, "Identifier")

    def test_a_save_keeps_the_identifier_it_already_had(self):
        response = self.client.post(
            reverse("control_plane:edit", kwargs={"key": "a-box"}),
            {"name": "a-box", "role": "A renamed purpose", "enabled": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.key, "a-box")
        self.assertEqual(self.resource.spec["role"], "A renamed purpose")
