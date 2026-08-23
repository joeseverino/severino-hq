"""Contract tests for HQ's shared connection registry."""

from dataclasses import replace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ProviderConnection

from .connections import (
    ConnectionAbility,
    ConnectionFact,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
    connection_catalog,
    connection_specs,
    describe_connections,
    list_connections,
)
from .command_center import command_center
from .security import Capability, Principal


READ = Principal("reader", "test", frozenset({Capability.READ}))
NONE = Principal("nobody", "test", frozenset())
FINANCE = Principal("finance", "test", frozenset({"example.finance.read"}))


def _finance_spec() -> ConnectionSpec:
    abilities = (
        ConnectionAbility(
            "accounts.read",
            "Read accounts",
            "Read account identity and balances.",
            required_scopes=("accounts:read",),
        ),
        ConnectionAbility(
            "transactions.sync",
            "Sync transactions",
            "Refresh transaction history.",
            effect="remote_write",
            required_scopes=("transactions:read",),
        ),
    )
    instance = ConnectionInstance(
        "capital-one",
        "Capital One",
        "plaid",
        "good",
        "healthy",
        granted_scopes=("accounts:read",),
        scopes_known=True,
        ability_names=("accounts.read", "transactions.sync"),
        targets=(ConnectionLink("4 accounts", "/example/accounts/"),),
        facts=(ConnectionFact("Last refresh", "12 hours ago"),),
    )
    return ConnectionSpec(
        "example.finance",
        "Financial institutions",
        "Linked institutions and their granted access.",
        "example.finance.read",
        lambda: (instance,),
        abilities,
        setup_route="dashboard",
        management_route="dashboard",
        secret_store="Plaid",
    )


class ConnectionExecutionTests(TestCase):
    def test_core_spec_derives_controller_abilities_and_safe_state(self):
        from django.utils import timezone

        ProviderConnection.objects.create(
            connection_ref="example-cloudflare",
            controller_id="controller",
            provider="cloudflare_dns",
            endpoint="https://api.cloudflare.com/client/v4",
            reaches=["example.com"],
            reachable=True,
            probed=True,
            detail="1 zone.",
            observed_at=timezone.now(),
        )

        outcome = list_connections(principal=READ)
        core = next(
            group
            for group in outcome["groups"]
            if group["name"] == "infrastructure.controllers"
        )
        connection = core["instances"][0]

        self.assertEqual(connection["label"], "example-cloudflare")
        self.assertIn("cloudflare.dns_record", {
            ability["name"] for ability in connection["abilities"]
        })
        self.assertNotIn("token", connection)
        self.assertNotIn("credential", connection)
        self.assertEqual(connection["controller_id"], "controller")

    def test_scope_coverage_is_derived_for_every_ability(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs",
            return_value=(_finance_spec(),),
        ):
            groups = connection_catalog(principal=FINANCE)

        finance = next(group for group in groups if group.spec.name == "example.finance")
        states = {
            state.ability.name: state
            for state in finance.connections[0].abilities
        }
        self.assertTrue(states["accounts.read"].available)
        self.assertFalse(states["transactions.sync"].available)
        self.assertEqual(
            states["transactions.sync"].missing_scopes,
            ("transactions:read",),
        )

    def test_unknown_scope_coverage_stays_unknown(self):
        spec = _finance_spec()
        instance = replace(spec.instance_provider()[0], scopes_known=False)
        spec = replace(spec, instance_provider=lambda: (instance,))
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            groups = connection_catalog(principal=FINANCE)

        states = groups[0].connections[0].abilities
        self.assertTrue(states)
        self.assertTrue(all(state.available is None for state in states))
        self.assertTrue(all(state.missing_scopes == () for state in states))

    def test_connection_families_authorize_before_calling_their_provider(self):
        provider = mock.Mock(return_value=())
        spec = ConnectionSpec(
            "example.private",
            "Private",
            "Private connection state.",
            "example.private.read",
            provider,
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            groups = connection_catalog(principal=NONE)

        self.assertEqual(groups, ())
        provider.assert_not_called()


class ConnectionRegistrationTests(TestCase):
    def test_one_spec_drives_machine_discovery(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs",
            return_value=(_finance_spec(),),
        ):
            described = describe_connections()

        finance = next(
            item for item in described["connections"]
            if item["name"] == "example.finance"
        )
        self.assertEqual(finance["secret_store"], "Plaid")
        self.assertEqual(
            finance["abilities"][1]["required_scopes"],
            ["transactions:read"],
        )
        self.assertEqual(finance["abilities"][1]["governs_kinds"], [])

    def test_command_center_pluralizes_one_ability(self):
        spec = _finance_spec()
        spec = replace(spec, abilities=spec.abilities[:1])
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            discovered = command_center("finance", principal=FINANCE)

        self.assertEqual(discovered["connections"][0].badges[0], "1 ability")

    def test_command_center_finds_a_connection_by_its_declared_ability(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            discovered = command_center("tailscale", principal=READ)

        self.assertEqual(
            [item.name for item in discovered["connections"]],
            ["infrastructure.controllers"],
        )
        self.assertIn("Tailnet device", discovered["connections"][0].badges)
        self.assertIn("Tailnet policy", discovered["connections"][0].badges)

    def test_command_center_caps_broad_ability_match_explanations(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            discovered = command_center("e", principal=READ)

        core = next(
            item
            for item in discovered["connections"]
            if item.name == "infrastructure.controllers"
        )
        self.assertEqual(len(core.badges), 6)
        self.assertRegex(core.badges[-1], r"^\+\d+ matching abilities$")

    def test_command_center_explains_terms_matched_across_abilities(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            discovered = command_center("tailnet proxy", principal=READ)

        core = discovered["connections"][0]
        self.assertIn("Proxy host", core.badges)
        self.assertIn("Tailnet device", core.badges)

    def test_command_center_does_not_leak_connection_abilities_without_read(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            discovered = command_center("tailscale", principal=NONE)

        self.assertEqual(discovered["connections"], ())

    def test_core_connection_describes_every_tailscale_ability(self):
        core = next(
            item
            for item in describe_connections()["connections"]
            if item["name"] == "infrastructure.controllers"
        )

        self.assertEqual(
            {
                ability["name"]
                for ability in core["abilities"]
                if ability["name"].startswith("tailscale.")
            },
            {"tailscale.device", "tailscale.policy"},
        )
        self.assertEqual(
            {
                ability["name"]: ability["governs_kinds"]
                for ability in core["abilities"]
                if ability["name"].startswith("tailscale.")
            },
            {
                "tailscale.device": ["tailscale.device"],
                "tailscale.policy": ["tailscale.policy"],
            },
        )

    def test_invalid_governed_resource_kinds_fail_at_composition(self):
        broken = replace(
            _finance_spec(),
            abilities=(
                replace(
                    _finance_spec().abilities[0],
                    governs_kinds=("not a dotted kind",),
                ),
            ),
        )
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(broken,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "invalid governed kinds"),
        ):
            connection_specs()

    def test_plugin_connection_names_cannot_shadow_core(self):
        duplicate = ConnectionSpec(
            "infrastructure.controllers",
            "Duplicate",
            "Must not silently replace the host contract.",
            Capability.READ,
            lambda: (),
        )
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs",
                return_value=(duplicate,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "Duplicate connection"),
        ):
            connection_specs()

    def test_unknown_instance_abilities_fail_closed(self):
        broken = ConnectionSpec(
            "example.broken",
            "Broken",
            "Broken instance contract.",
            Capability.READ,
            lambda: (
                ConnectionInstance(
                    "one",
                    "One",
                    "example",
                    "good",
                    "healthy",
                    ability_names=("example.missing",),
                ),
            ),
        )
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs",
                return_value=(broken,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "unknown abilities"),
        ):
            connection_catalog(principal=READ)

    def test_unsafe_relationship_urls_fail_closed(self):
        broken = ConnectionSpec(
            "example.broken",
            "Broken",
            "Broken instance contract.",
            "example.finance.read",
            lambda: (
                ConnectionInstance(
                    "one",
                    "One",
                    "example",
                    "good",
                    "healthy",
                    targets=(ConnectionLink("Bad", "javascript:alert(1)"),),
                ),
            ),
        )
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs",
                return_value=(broken,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "invalid relationship"),
        ):
            connection_catalog(principal=FINANCE)

    def test_credential_userinfo_in_an_endpoint_fails_closed(self):
        spec = _finance_spec()
        instance = replace(
            spec.instance_provider()[0],
            endpoint="https://operator:secret@example.test/api",
        )
        spec = replace(spec, instance_provider=lambda: (instance,))
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "private URL parts"),
        ):
            list_connections(principal=FINANCE)

    def test_endpoint_queries_fail_closed_before_any_adapter_can_render_them(self):
        spec = _finance_spec()
        instance = replace(
            spec.instance_provider()[0],
            endpoint="https://api.example.test/link?access_token=secret",
        )
        spec = replace(spec, instance_provider=lambda: (instance,))
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "private URL parts"),
        ):
            list_connections(principal=FINANCE)

    def test_blank_controller_identity_fails_closed(self):
        spec = _finance_spec()
        instance = replace(spec.instance_provider()[0], controller_id=" ")
        spec = replace(spec, instance_provider=lambda: (instance,))
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "invalid controller id"),
        ):
            list_connections(principal=FINANCE)

    def test_non_datetime_observations_fail_before_serialization(self):
        spec = _finance_spec()
        instance = replace(spec.instance_provider()[0], observed_at=object())
        spec = replace(spec, instance_provider=lambda: (instance,))
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "observation time"),
        ):
            list_connections(principal=FINANCE)

    def test_documentation_urls_are_restricted_to_safe_destinations(self):
        spec = replace(_finance_spec(), documentation_url="javascript:alert(1)")
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "documentation URL"),
        ):
            describe_connections()


class ConnectionWorkspaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def test_a_plugin_spec_renders_without_a_host_template_change(self):
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs",
                return_value=(_finance_spec(),),
            ),
            mock.patch("control_plane.views.web_principal", return_value=FINANCE),
        ):
            response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Financial institutions")
        self.assertContains(response, "Capital One")
        self.assertContains(response, "Sync transactions")
        self.assertContains(response, "Needs scope")
        self.assertContains(response, "Secrets in Plaid")
        self.assertContains(response, "Security posture")
        self.assertContains(response, "Security controls and proof")
        self.assertContains(response, "External edge")

    def test_a_plugin_unclassified_kind_does_not_trigger_controller_prose(self):
        spec = _finance_spec()
        instance = replace(spec.instance_provider()[0], kind="unclassified")
        spec = replace(spec, instance_provider=lambda: (instance,))
        with (
            mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ),
            mock.patch("control_plane.views.web_principal", return_value=FINANCE),
        ):
            response = self.client.get(reverse("control_plane:connections"))

        self.assertNotContains(response, "Not yet classified")
