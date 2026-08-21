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
from control_plane.providers import NameContext

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

        choices = container_stack(NameContext())
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

        self.assertEqual(zone(NameContext())["connection_ref"], (("cloudflare-dns",) * 2,))

    def test_a_broken_connection_is_offered_last_and_says_so(self):
        """Still offered, because it is the one that already exists.

        Ordering puts a working credential first; omitting the broken one would
        make the menu disagree with the page that lists it.
        """

        sweep(
            {**A_PORTAINER, "connection_ref": "a-broken-one", "ok": False},
            A_PORTAINER,
        )

        refs = [ref for ref, _ in container_stack(NameContext())["connection_ref"]]
        labels = dict(container_stack(NameContext())["connection_ref"])
        self.assertEqual(refs, ["homelab-portainer", "a-broken-one"])
        self.assertIn("not answering", labels["a-broken-one"])

    def test_menus_are_empty_before_the_first_sweep(self):
        """And the fields stay typeable. An empty menu is a smaller failure
        than one that cannot describe what already exists."""

        self.assertEqual(container_stack(NameContext())["host"], ())
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


class OfferTests(TestCase):
    """What HQ offers for a name, given what it can actually reach.

    An offer that cannot work is worse than no offer. Declaring one is how you
    find out, and the finding out arrives a minute later in a failed job.
    """

    def test_a_name_in_no_reachable_zone_is_not_offered_public_dns(self):
        sweep(A_DNS_TOKEN)

        offers = dict(_certificate_and_dns("probe.homelab")["dns"].declarable)

        self.assertNotIn("cloudflare.dns_record", offers)
        self.assertIn("adguard.rewrite", offers)

    def test_it_says_why_rather_than_dropping_the_option(self):
        sweep(A_DNS_TOKEN)

        refused = dict(_certificate_and_dns("probe.homelab")["certificate"].unavailable)

        self.assertIn("TLS certificate", refused)
        self.assertIn("Upload a certificate instead", refused["TLS certificate"])

    def test_the_option_that_works_is_offered_in_its_place(self):
        sweep(A_DNS_TOKEN)

        offers = dict(
            _certificate_and_dns("probe.homelab")["certificate"].declarable
        )

        self.assertIn("tls.uploaded_certificate", offers)

    def test_a_name_in_a_reachable_zone_keeps_both(self):
        sweep(A_DNS_TOKEN)

        facets = _certificate_and_dns("probe.example.com")

        self.assertIn("cloudflare.dns_record", dict(facets["dns"].declarable))
        self.assertIn("tls.certificate", dict(facets["certificate"].declarable))

    def test_nothing_is_refused_before_a_sweep(self):
        """An empty report means nobody looked, not that nothing is reachable.

        Read as a prohibition, one missed sweep would present as a deliberate
        restriction on every public name HQ has.
        """

        facets = _certificate_and_dns("probe.example.com")

        self.assertIn("cloudflare.dns_record", dict(facets["dns"].declarable))
        self.assertEqual(facets["certificate"].unavailable, ())


def _certificate_and_dns(hostname):
    from .naming import name_context
    from .services import Facet

    context = name_context(hostname)
    return {
        facet: Facet(id=facet, label=facet.title(), context=context)
        for facet in ("dns", "certificate")
    }


class SeedTests(TestCase):
    """What a form opens already knowing.

    Each of these was on the screen when the question was asked, and the
    operator was reading it off one card and typing it into the next.
    """

    def setUp(self):
        from control_plane.models import ManagedResource, TopologySnapshot

        TopologySnapshot.objects.update_or_create(
            pk="topology",
            defaults={
                "schema_version": 1,
                "checksum": "test",
                "payload": {
                    "hosts": [{"id": "homelab-server", "lan_ip": "192.168.1.233"}]
                },
            },
        )
        ManagedResource.objects.create(
            key="probe-stack",
            kind="portainer.stack",
            spec={
                "connection_ref": "homelab-portainer",
                "host": "homelab-server",
                "name": "probe",
                "compose": "services: {}",
                "environment": [],
                "hostnames": ["probe.homelab"],
                "port": 8099,
            },
        )

    def test_a_proxy_points_at_what_already_serves_the_name(self):
        from control_plane.providers import PROVIDERS

        from .naming import name_context

        seeded = PROVIDERS["npm.proxy_host"].seed(name_context("probe.homelab"))

        self.assertEqual(seeded["forward_port"], 8099)

    def test_it_uses_an_address_the_proxy_can_resolve(self):
        """Not the machine's name. Nginx has never heard of `homelab-server`,
        so seeding it puts a plausible value in the box that cannot work."""

        from control_plane.providers import PROVIDERS

        from .naming import name_context

        seeded = PROVIDERS["npm.proxy_host"].seed(name_context("probe.homelab"))

        self.assertEqual(seeded["forward_host"], "192.168.1.233")

    def test_nothing_is_seeded_when_nothing_serves_the_name(self):
        from control_plane.providers import PROVIDERS

        from .naming import name_context

        seeded = PROVIDERS["npm.proxy_host"].seed(name_context("nowhere.homelab"))

        self.assertNotIn("forward_host", seeded)
        self.assertEqual(seeded["domain_names"], ["nowhere.homelab"])

    def test_a_public_record_seeds_the_zone_the_credential_holds(self):
        """Rather than the last two labels, which is a guess that is wrong for
        every co.uk and right only by the shape of most names."""

        from control_plane.providers import PROVIDERS

        from .naming import name_context

        sweep({**A_DNS_TOKEN, "reaches": ["dev.example.com"]})
        seeded = PROVIDERS["cloudflare.dns_record"].seed(
            name_context("probe.dev.example.com")
        )

        self.assertEqual(seeded["zone"], "dev.example.com")


class AdoptionSafetyTests(TestCase):
    """HQ offers to take on a container only when that would not build a second.

    Everything running today was started by compose on the machine, so Portainer
    holds no stack for any of it. A declaration built as though it did would ask
    Portainer to stand up its own copy of something already serving.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def _service_page(self, portainer_managed):
        from control_plane.models import ManagedResource, ProviderInventory
        from django.utils import timezone

        ProviderInventory.objects.update_or_create(
            kind="portainer.stack",
            defaults={
                "records": [
                    {
                        "name": "probe",
                        "stack": "probe",
                        "host": "homelab-server",
                        "ports": [8099],
                        "state": "running",
                        "portainer_managed": portainer_managed,
                    }
                ],
                "reachable": True,
                "observed_at": timezone.now(),
            },
        )
        ManagedResource.objects.create(
            key="probe-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["probe.homelab"],
                "forward_scheme": "http",
                "forward_host": "192.168.1.233",
                "forward_port": 8099,
                "certificate_resource": "",
                "ssl_forced": True,
                "http2": True,
                "websocket": True,
                "caching_enabled": False,
                "block_exploits": True,
                "access_list_id": 0,
                "advanced_config": "",
                "hsts_enabled": True,
                "hsts_subdomains": True,
                "trust_forwarded_proto": True,
                "serving": True,
            },
        )
        from control_plane.models import TopologySnapshot

        TopologySnapshot.objects.update_or_create(
            pk="topology",
            defaults={
                "schema_version": 1,
                "checksum": "test",
                "payload": {
                    "hosts": [{"id": "homelab-server", "lan_ip": "192.168.1.233"}]
                },
            },
        )
        return self.client.get(
            reverse("control_plane:service", kwargs={"hostname": "probe.homelab"})
        )

    def test_a_container_portainer_did_not_create_is_not_offered_for_adoption(self):
        response = self._service_page(portainer_managed=False)

        self.assertContains(response, "watch it but not take it over")
        self.assertNotContains(response, "Manage as container stack")

    def test_one_portainer_created_can_be_taken_on(self):
        response = self._service_page(portainer_managed=True)

        self.assertContains(response, "Manage as container stack")

    def test_either_way_it_reports_what_is_running(self):
        response = self._service_page(portainer_managed=False)

        self.assertContains(response, "probe")
        self.assertContains(response, "watch it but not take it over")


class ControllerColumnTests(TestCase):
    """Whose controller reported a connection is worth saying once there are two.

    Printed under every row it repeated one machine's name seven times, which on
    a phone was a third of the page saying nothing.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def test_one_controller_is_not_named_on_every_row(self):
        sweep(A_PORTAINER, A_DNS_TOKEN, controller_id="homelab-server")

        response = self.client.get(reverse("control_plane:connections"))

        self.assertNotContains(response, "via homelab-server")

    def test_two_controllers_are_told_apart(self):
        sweep(A_PORTAINER, controller_id="homelab-server")
        sweep(A_DNS_TOKEN, controller_id="edge")

        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "via homelab-server")
        self.assertContains(response, "via edge")
