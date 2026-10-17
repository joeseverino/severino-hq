from io import StringIO

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.credential_reads import MINTERS, observer_permissions

from ..connection_setup import setups
from .host_only import host_only


class DeclarationTests(TestCase):
    def test_every_provider_says_what_to_create_and_where(self):
        for provider, kind in CONNECTION_KINDS.items():
            with self.subTest(provider=provider):
                self.assertTrue(kind.create.endswith("."), "one sentence saying what to make and where")
                self.assertTrue(kind.create_url.startswith("https://"), "the provider's own page on it")


class SetupTests(TestCase):
    def by_provider(self, exclude=frozenset()):
        return {setup.provider: setup for setup in setups(exclude)}

    def test_a_fresh_install_is_shown_every_provider(self):
        self.assertEqual(set(self.by_provider()), set(CONNECTION_KINDS))

    def test_a_connected_provider_is_not_offered_again(self):
        self.assertNotIn("portainer", self.by_provider(frozenset({"portainer"})))

    def test_the_item_carries_the_three_fields_that_make_it_a_connection_then_its_shape(self):
        setup = self.by_provider()["tailscale"]
        names = [field.name for field in setup.fields]

        self.assertEqual(names[:3], ["connection_ref", "projection", "env_prefix"])
        values = {field.name: field.value for field in setup.fields}
        self.assertEqual(values["projection"], "oauth_client")
        self.assertEqual(values["env_prefix"], "TAILSCALE")
        self.assertIn("credential", names)
        self.assertEqual(setup.item_type, "API Credential")
        self.assertTrue(next(f for f in setup.fields if f.name == "credential").secret)

    def test_a_scoped_credential_lists_its_reads_and_declared_writes(self):
        dns = self.by_provider()["cloudflare_dns"]
        api = self.by_provider()["cloudflare_api"]

        self.assertIn("DNS Write (zone)", dns.permissions)
        # Its reads, and D1 Write for the contact inbox's changes.
        self.assertEqual(set(api.permissions), {*observer_permissions("cloudflare_api"), "D1 Write (account)"})
        self.assertEqual(api.mint, MINTERS["cloudflare_api"].script)

    def test_a_coarse_login_lists_no_permissions(self):
        npm = self.by_provider()["npm"]

        self.assertEqual(npm.permissions, ())
        self.assertEqual(npm.item_type, "Login")


class ReferenceTests(TestCase):
    def test_the_committed_reference_is_what_the_providers_declare(self):
        from django.core.management import call_command

        call_command("connections_reference", "--check", stdout=StringIO())

    def test_an_item_is_named_with_its_article(self):
        by_provider = {setup.provider: setup for setup in setups()}

        self.assertEqual(by_provider["npm"].item, "a Login item")
        self.assertEqual(by_provider["portainer"].item, "an API Credential item")
        self.assertEqual(by_provider["ssh"].item, "an item of any type")


class PageTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(get_user_model().objects.create_superuser("setup-reader"))

    def test_a_fresh_install_walks_through_connecting_each_provider(self):
        with override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/hq"):
            response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Connect a service")
        for kind in CONNECTION_KINDS.values():
            self.assertContains(response, escape(kind.create))
        self.assertContains(response, 'href="https://github.com/example/hq/blob/main/docs/Secrets.md#connections"')

    def test_a_connected_provider_is_listed_above_not_offered_below(self):
        from hq.domains.control_plane.models import ProviderConnection

        ProviderConnection.objects.create(
            connection_ref="example-portainer",
            controller_id="example-controller",
            provider="portainer",
            endpoint="https://portainer.example.com",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        response = self.client.get(reverse("control_plane:connections"))

        self.assertNotContains(response, escape(CONNECTION_KINDS["portainer"].create))
        self.assertContains(response, escape(CONNECTION_KINDS["npm"].create))

    def test_with_every_provider_connected_a_connection_can_still_be_added(self):
        self.enterContext(host_only())
        from hq.domains.control_plane.models import ProviderConnection

        for provider in CONNECTION_KINDS:
            ProviderConnection.objects.create(
                connection_ref=f"example-{provider.replace('_', '-')}",
                controller_id="example-controller",
                provider=provider,
                endpoint="https://service.example.com",
                reachable=True,
                probed=True,
                observed_at=timezone.now(),
            )

        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Connect another account")
        self.assertContains(response, escape(CONNECTION_KINDS["portainer"].create))
        self.assertContains(response, 'href="/infrastructure/connections/#connection-setup-heading"')

    def test_the_way_to_add_one_leads_the_page(self):
        response = self.client.get(reverse("control_plane:connections"))

        actions = response.context["page"].actions
        self.assertEqual((actions[0].label, actions[0].primary), ("Add a connection", True))
