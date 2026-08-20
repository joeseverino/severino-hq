"""The connection sweep: what HQ can reach, and everything derived from it.

Two properties hold this together. HQ never stores a credential -- only the
report that one exists and what it answered. And every menu asking "which
machine" or "which domain" is derived from that report, so an estate grows by
being given a credential rather than by anything here being edited.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ProviderConnection
from control_plane.providers import PROVIDERS

from .connections import connection_readings, connections_for, reachable_through
from .inventory import record_connections
from .provider_choices import container_stack, zone
from .security import cli_principal


A_PORTAINER = {
    "connection_ref": "homelab-portainer",
    "provider": "portainer",
    "endpoint": "https://admin.homelab",
    "reaches": ["homelab-server", "sl-cloud-edge-01"],
    "ok": True,
    "probed": True,
    "detail": "2 of 2 environments reachable.",
}
A_DNS_TOKEN = {
    "connection_ref": "cloudflare-dns",
    "provider": "cloudflare_dns",
    "endpoint": "https://api.cloudflare.com/client/v4",
    "reaches": ["example.com", "example.net"],
    "ok": True,
    "probed": True,
    "detail": "2 zones.",
}


def sweep(*connections, controller_id="homelab-server"):
    return record_connections(
        list(connections), principal=cli_principal(), controller_id=controller_id
    )


class RecordingTests(TestCase):
    def test_a_sweep_replaces_the_last_one_for_that_controller(self):
        """A credential removed from the vault stops being offered.

        Merged instead of replaced, a connection deleted in 1Password would keep
        appearing in every menu in HQ, which is the one thing a cache of an
        observation must not do.
        """

        sweep(A_PORTAINER, A_DNS_TOKEN)
        sweep(A_PORTAINER)

        self.assertEqual(
            [row.connection_ref for row in ProviderConnection.objects.all()],
            ["homelab-portainer"],
        )

    def test_one_controller_does_not_erase_another(self):
        """Two controllers render two vaults, and a ref means what its own says.

        Scoped by controller because the alternative is that whichever swept
        last deletes the other's connections, which would make a second machine
        take the first one's menus down every fifteen minutes.
        """

        sweep(A_PORTAINER, controller_id="homelab-server")
        sweep(A_DNS_TOKEN, controller_id="edge")

        self.assertEqual(ProviderConnection.objects.count(), 2)

    def test_an_unreachable_connection_is_kept_and_marked(self):
        """A broken credential is still the credential the operator has.

        Dropped from the sweep, an expired token reads as "you never set this
        up" -- so the page invites adding a second one, and the real problem
        stays invisible.
        """

        sweep({**A_DNS_TOKEN, "ok": False, "detail": "Token is not valid."})

        reading = connection_readings()[0]
        self.assertEqual(reading.status, "unreachable")
        self.assertEqual(reading.detail, "Token is not valid.")

    def test_an_unprobed_connection_is_not_a_broken_one(self):
        sweep({**A_PORTAINER, "probed": False, "detail": "No probe."})

        self.assertEqual(connection_readings()[0].status, "unprobed")

    def test_a_connection_without_a_ref_is_skipped_rather_than_stored(self):
        sweep({"provider": "portainer"}, A_PORTAINER)

        self.assertEqual(ProviderConnection.objects.count(), 1)


class DerivationTests(TestCase):
    def test_what_a_connection_is_for_comes_from_the_providers(self):
        """Not stored, so a provider added tomorrow lists itself here.

        The alternative is a table of "portainer means container stacks" kept
        beside the providers that already say so.
        """

        sweep(A_PORTAINER)

        self.assertEqual(connection_readings()[0].supplies, ("Container stack",))

    def test_an_unclassified_connection_claims_nothing(self):
        sweep({**A_PORTAINER, "provider": ""})

        self.assertEqual(connection_readings()[0].supplies, ())

    def test_every_provider_names_connections_the_controller_can_report(self):
        """No provider asks for a kind of connection that cannot exist.

        A typo here is invisible until a form renders an empty menu, at which
        point it looks like a missing credential rather than a missing letter.
        """

        named = {
            provider
            for spec in PROVIDERS.values()
            for provider in spec.connection_providers
        }

        self.assertEqual(
            named - {"adguard", "npm", "cloudflare_dns", "portainer", "ssh"}, set()
        )

    def test_where_a_stack_can_run_is_what_a_portainer_reaches(self):
        """A machine is a place to run something because a Portainer holds it.

        Read from the connection rather than from containers found on it, so a
        machine running nothing is still offered -- which is exactly when this
        form is being filled in.
        """

        sweep(A_PORTAINER)

        choices = container_stack()
        self.assertEqual(
            [value for value, _ in choices["host"]],
            ["homelab-server", "sl-cloud-edge-01"],
        )
        self.assertEqual(choices["connection_ref"], (("homelab-portainer",) * 2,))

    def test_a_machine_behind_two_portainers_is_offered_once(self):
        sweep(
            A_PORTAINER,
            {
                **A_PORTAINER,
                "connection_ref": "second-portainer",
                "reaches": ["homelab-server"],
            },
        )

        self.assertEqual(
            [name for name, _ in reachable_through("portainer")],
            ["homelab-server", "sl-cloud-edge-01"],
        )

    def test_which_domains_can_be_declared_is_what_the_token_may_edit(self):
        sweep(A_DNS_TOKEN)

        self.assertEqual(zone()["connection_ref"], (("cloudflare-dns",) * 2,))

    def test_a_broken_connection_is_offered_last_and_says_so(self):
        """Still offered, because it is the one that already exists.

        Ordering puts a working credential first; omitting the broken one would
        make the menu disagree with the page that lists it.
        """

        sweep(
            {**A_PORTAINER, "connection_ref": "a-broken-one", "ok": False},
            A_PORTAINER,
        )

        refs = [ref for ref, _ in container_stack()["connection_ref"]]
        labels = dict(container_stack()["connection_ref"])
        self.assertEqual(refs, ["homelab-portainer", "a-broken-one"])
        self.assertIn("not answering", labels["a-broken-one"])

    def test_menus_are_empty_before_the_first_sweep(self):
        """And the fields stay typeable. An empty menu is a smaller failure
        than one that cannot describe what already exists."""

        self.assertEqual(container_stack()["host"], ())
        self.assertEqual(connections_for("portainer"), ())


class ConnectionPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def test_the_page_lists_what_the_controller_reported(self):
        sweep(A_PORTAINER, A_DNS_TOKEN)

        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "homelab-portainer")
        self.assertContains(response, "sl-cloud-edge-01")
        self.assertContains(response, "Container stack")

    def test_the_page_never_carries_a_secret(self):
        """It cannot, because nothing here has one -- and this is the assertion
        that keeps it that way if a field is ever added to the report."""

        sweep({**A_PORTAINER, "detail": "2 of 2 environments reachable."})

        response = self.client.get(reverse("control_plane:connections"))

        self.assertNotContains(response, "API_TOKEN")
        self.assertNotContains(response, "PASSWORD")

    def test_it_says_so_when_nothing_has_swept(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "No controller has reported yet")

    def test_an_unclassified_connection_is_named_rather_than_hidden(self):
        sweep({**A_PORTAINER, "provider": ""})

        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Not yet classified")
        self.assertContains(response, "homelab-portainer")
