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
    describe_connections,
    list_connections,
)
from .integrations import integration_graph
from .command_center import command_center
from .security import Capability, Principal


READ = Principal("reader", "test", frozenset({Capability.READ}))
NONE = Principal("nobody", "test", frozenset())
FINANCE = Principal("finance", "test", frozenset({"example.finance.read"}))
FINANCE_OPERATOR = Principal(
    "finance-operator",
    "test",
    frozenset({"example.finance.read", Capability.WRITE_PROJECTS}),
)
INFRA_OPERATOR = Principal(
    "infrastructure-operator",
    "test",
    frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}),
)


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
        "aggregator",
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
        secret_store="Example Vault",
    )


class ConnectionExecutionTests(TestCase):
    def test_hyphenated_plugin_route_names_are_valid(self):
        spec = replace(
            _finance_spec(),
            web_route="example:connection-list",
            management_route="example:connection-list",
        )

        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            self.assertIn(spec, integration_graph().connections.values())

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
        self.assertIn(
            "cloudflare.dns_record",
            {ability["name"] for ability in connection["abilities"]},
        )
        self.assertNotIn("token", connection)
        self.assertNotIn("credential", connection)
        self.assertEqual(connection["controller_id"], "controller")

    def test_scope_coverage_is_derived_for_every_ability(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs",
            return_value=(_finance_spec(),),
        ):
            groups = connection_catalog(principal=FINANCE)

        finance = next(
            group for group in groups if group.spec.name == "example.finance"
        )
        states = {
            state.ability.name: state for state in finance.connections[0].abilities
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

    def test_an_ability_without_provider_scopes_is_capability_authorized(self):
        spec = _finance_spec()
        ability = replace(spec.abilities[0], required_scopes=())
        instance = replace(
            spec.instance_provider()[0],
            scopes_known=False,
            ability_names=(ability.name,),
        )
        spec = replace(
            spec,
            abilities=(ability,),
            instance_provider=lambda: (instance,),
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            state = connection_catalog(principal=FINANCE)[0].connections[0].abilities[0]

        self.assertTrue(state.available)
        self.assertEqual(state.missing_scopes, ())
        # Usable under HQ's own authorization, and honest about why: the
        # ability declared no proof and the instance reported no credential kind.
        self.assertEqual(state.evidence, "undeclared")
        self.assertFalse(state.proven)

    def test_missing_scope_derives_one_real_management_next_step(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs",
            return_value=(_finance_spec(),),
        ):
            connection = connection_catalog(principal=FINANCE)[0].connections[0]

        action = connection.recommended_action
        self.assertIsNotNone(action)
        self.assertEqual(action.name, "manage")
        self.assertEqual(action.label, "Review access")
        self.assertIn("1 required provider scope is missing", action.reason)

    def test_a_command_link_requires_scope_evidence_and_hq_authority(self):
        base = _finance_spec()
        ability = replace(base.abilities[0], capability="project.create")
        instance = replace(
            base.instance_provider()[0],
            ability_names=(ability.name,),
        )
        spec = replace(
            base,
            abilities=(ability,),
            instance_provider=lambda: (instance,),
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            reader = connection_catalog(principal=FINANCE)[0].connections[0]
            operator = connection_catalog(principal=FINANCE_OPERATOR)[0].connections[0]

        self.assertIsNone(reader.abilities[0].action)
        action = operator.abilities[0].action
        self.assertIsNotNone(action)
        self.assertEqual(action.capability, "project.create")
        self.assertEqual(action.url, "/commands/project.create/")

    def test_machine_catalog_emits_the_same_safe_actions_as_the_web_projection(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs",
            return_value=(_finance_spec(),),
        ):
            payload = list_connections(principal=FINANCE)

        instance = payload["groups"][0]["instances"][0]
        self.assertEqual(
            [action["name"] for action in instance["actions"]],
            ["open", "manage", "relationships"],
        )
        self.assertTrue(
            next(
                action["recommended"]
                for action in instance["actions"]
                if action["name"] == "manage"
            )
        )
        self.assertNotIn("secret", str(instance["actions"]).casefold())

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
            item
            for item in described["connections"]
            if item["name"] == "example.finance"
        )
        self.assertEqual(finance["secret_store"], "Example Vault")
        self.assertEqual(
            finance["abilities"][1]["required_scopes"],
            ["transactions:read"],
        )
        self.assertEqual(finance["abilities"][1]["governs_kinds"], [])

    def test_command_center_pluralizes_one_ability(self):
        spec = _finance_spec()
        instance = replace(
            spec.instance_provider()[0], ability_names=(spec.abilities[0].name,)
        )
        spec = replace(
            spec,
            abilities=spec.abilities[:1],
            instance_provider=lambda: (instance,),
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            discovered = command_center("finance", principal=FINANCE)

        self.assertEqual(discovered["connections"][0].badges[0], "1 ability")

    def test_command_center_finds_the_live_instance_not_only_its_family(self):
        spec = _finance_spec()
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            discovered = command_center(
                "capital", principal=FINANCE, include_live_connections=True
            )

        self.assertEqual(discovered["connections"][0].label, "Capital One")
        self.assertEqual(discovered["connections"][0].badges, ("healthy", "2 abilities"))

    def test_two_letter_query_matches_word_starts_not_word_middles(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            discovered = command_center("sp", principal=READ)

        self.assertNotIn(
            "infrastructure.resource.create",
            {item.name for item in discovered["commands"]},
        )

    def test_command_center_finds_a_connection_by_its_declared_ability(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            discovered = command_center("tailscale", principal=READ)

        self.assertEqual(
            [item.name for item in discovered["connections"]],
            ["infrastructure.controllers"],
        )
        self.assertIn("Tailnet device", discovered["connections"][0].badges)
        self.assertIn("Tailnet policy", discovered["connections"][0].badges)

    def test_matching_ability_derives_its_authorized_commands(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            discovered = command_center("tailscale", principal=INFRA_OPERATOR)

        commands = {item.name: item for item in discovered["commands"]}
        self.assertEqual(
            set(commands),
            {
                "infrastructure.resource.create",
                "infrastructure.resource.update",
                "infrastructure.reconcile",
                "infrastructure.resource.remove",
                # Scoped to the device kind, so it surfaces here for the same
                # reason `certificate.renew` does not: the ability names what
                # the credential reaches, and the command names what it acts on.
                "tailnet.routes.approve",
            },
        )
        self.assertIn("via Tailnet device", commands["infrastructure.reconcile"].badges)
        self.assertIn(
            "via Tailnet device", commands["tailnet.routes.approve"].badges
        )
        self.assertEqual(
            commands["infrastructure.reconcile"].url,
            "/commands/infrastructure.reconcile/"
            "?kind=tailscale.device&kind=tailscale.policy",
        )
        self.assertNotIn("certificate.renew", commands)

    def test_command_center_caps_broad_ability_match_explanations(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            discovered = command_center("e", principal=READ)

        core = next(
            item
            for item in discovered["connections"]
            if item.name == "infrastructure.controllers"
        )
        self.assertEqual(len(core.badges), 6)
        self.assertRegex(core.badges[-1], r"^\+\d+ matching abilities$")

    def test_command_center_explains_terms_matched_across_abilities(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            discovered = command_center("tailnet proxy", principal=READ)

        core = discovered["connections"][0]
        self.assertIn("Proxy host", core.badges)
        self.assertIn("Tailnet device", core.badges)

    def test_command_center_does_not_leak_connection_abilities_without_read(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
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
        self.assertTrue(
            all(
                ability["subject_resource"] == "infrastructure.resources"
                for ability in core["abilities"]
                if ability["name"].startswith("tailscale.")
            )
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
            integration_graph()

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
            integration_graph()

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
        self.assertContains(response, "Scope missing")
        self.assertContains(response, "Secrets in Example Vault")
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


class GrantEvidenceTests(TestCase):
    """Permission is an evidence-backed relationship, derived from two declarations."""

    def ability(self, **overrides):
        base = dict(
            name="example.read", label="Read", summary="Read one example."
        )
        return ConnectionAbility(**{**base, **overrides})

    def instance(self, **overrides):
        base = dict(id="one", label="One", kind="example", status="good", status_label="ok")
        return ConnectionInstance(**{**base, **overrides})

    def evidence(self, ability, instance):
        from application.connections import grant_evidence

        return grant_evidence(ability, instance)

    def test_every_pair_of_declarations_has_one_answer(self):
        scoped = self.ability(required_scopes=("a:read",), grant="scoped")
        cases = {
            # A rejected credential proves nothing, whatever the ability asked.
            (scoped, self.instance(credential_model="rejected", scopes_known=True, granted_scopes=("a:read",))): "revoked",
            # Keyless on either side: nothing to prove.
            (self.ability(grant="none"), self.instance(credential_model="coarse")): "not_applicable",
            (self.ability(), self.instance(credential_model="none")): "not_applicable",
            # Whole-account on either side satisfies anything, and says so.
            (self.ability(grant="coarse"), self.instance()): "coarse",
            (scoped, self.instance(credential_model="coarse")): "coarse",
            # A scoped requirement is checked scope by scope, when it can be.
            (scoped, self.instance(credential_model="scoped", scopes_known=True, granted_scopes=("a:read",))): "verified",
            (scoped, self.instance(credential_model="scoped", scopes_known=True, granted_scopes=())): "missing",
            (scoped, self.instance(credential_model="scoped")): "unknown",
            # Nothing required: the provider could have been asked, or nothing is known.
            (self.ability(), self.instance(credential_model="scoped")): "unverified",
            (self.ability(), self.instance()): "undeclared",
        }
        for (ability, instance), expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(self.evidence(ability, instance)[0], expected)

    def test_only_missing_names_what_is_missing(self):
        scoped = self.ability(required_scopes=("a:read", "b:write"), grant="scoped")
        state, missing = self.evidence(
            scoped, self.instance(scopes_known=True, granted_scopes=("a:read",))
        )
        self.assertEqual((state, missing), ("missing", ("b:write",)))

    def test_authority_and_lifecycle_follow_the_evidence(self):
        from datetime import timedelta

        from django.utils import timezone

        from application.connections import (
            ConnectionAbilityState,
            connection_authority,
            connection_lifecycle,
        )

        def states(*evidence):
            return tuple(
                ConnectionAbilityState(self.ability(), True, (), None, item)
                for item in evidence
            )

        self.assertEqual(connection_authority(()), "none")
        self.assertEqual(connection_authority(states("verified", "coarse")), "proven")
        self.assertEqual(connection_authority(states("verified", "undeclared")), "undeclared")
        self.assertEqual(connection_authority(states("unverified", "unknown")), "unknown")
        self.assertEqual(connection_authority(states("verified", "missing")), "missing")

        now = timezone.now()
        fresh = self.instance(observed_at=now - timedelta(minutes=5))
        old = self.instance(observed_at=now - timedelta(hours=30))
        life = lambda instance, authority, hours=24: connection_lifecycle(  # noqa: E731
            instance, authority, stale_after_hours=hours, now=now
        )
        self.assertEqual(life(self.instance(credential_model="rejected"), "proven"), "revoked")
        self.assertEqual(life(self.instance(), "proven"), "configured")
        self.assertEqual(life(old, "proven"), "stale")
        self.assertEqual(life(old, "proven", hours=48), "ready")
        self.assertEqual(life(fresh, "missing"), "unauthorized")
        self.assertEqual(life(self.instance(status="serious", observed_at=now), "proven"), "unreachable")
        self.assertEqual(life(self.instance(status="neutral", observed_at=now), "proven"), "configured")
        self.assertEqual(life(fresh, "proven"), "ready")
        self.assertEqual(life(fresh, "undeclared"), "reachable")

    def test_a_grant_model_must_agree_with_its_scopes(self):
        from application.integration_validation import validate_connection_spec

        def spec(ability):
            return ConnectionSpec(
                "example.grants", "Grants", "Grant contract.", Capability.READ,
                lambda: (), (ability,),
            )

        with self.assertRaisesRegex(ImproperlyConfigured, "invalid grant model"):
            validate_connection_spec(spec(self.ability(grant="admin")))
        with self.assertRaisesRegex(ImproperlyConfigured, "requires no scopes"):
            validate_connection_spec(spec(self.ability(grant="scoped")))
        with self.assertRaisesRegex(ImproperlyConfigured, "lists required scopes"):
            validate_connection_spec(
                spec(self.ability(grant="none", required_scopes=("a:read",)))
            )
        with self.assertRaisesRegex(ImproperlyConfigured, "staleness window"):
            validate_connection_spec(
                replace(spec(self.ability(grant="none")), stale_after_hours=0)
            )
        validate_connection_spec(spec(self.ability(grant="scoped", required_scopes=("a:read",))))

    def test_a_credential_model_is_validated_when_the_instance_is_read(self):
        def catalog(instance):
            spec = ConnectionSpec(
                "example.credentials", "Credentials", "Credential contract.",
                Capability.READ, lambda: (instance,), (self.ability(),),
            )
            with mock.patch(
                "application.plugins.plugin_connection_specs", return_value=(spec,)
            ):
                return connection_catalog(principal=READ)

        with self.assertRaisesRegex(ImproperlyConfigured, "invalid credential model"):
            catalog(self.instance(credential_model="password", ability_names=("example.read",)))

    def test_a_keyless_instance_cannot_also_report_grants(self):
        spec = ConnectionSpec(
            "example.credentials", "Credentials", "Credential contract.",
            Capability.READ,
            lambda: (self.instance(credential_model="none", scopes_known=True, ability_names=("example.read",)),),
            (self.ability(),),
        )
        with (
            mock.patch("application.plugins.plugin_connection_specs", return_value=(spec,)),
            self.assertRaisesRegex(ImproperlyConfigured, "keyless but reports grants"),
        ):
            connection_catalog(principal=READ)

    def test_controller_connections_carry_their_providers_credential_model(self):
        from django.utils import timezone

        for provider, expected in (("ssh", "coarse"), ("cloudflare_dns", "scoped")):
            ProviderConnection.objects.create(
                connection_ref=f"a-{provider}",
                controller_id="controller",
                provider=provider,
                reaches=["example.test"],
                reachable=True,
                probed=True,
                observed_at=timezone.now(),
            )

        outcome = list_connections(principal=READ)
        core = next(g for g in outcome["groups"] if g["name"] == "infrastructure.controllers")
        by_ref = {item["label"]: item for item in core["instances"]}

        # A login key is the whole machine: proven, and ready.
        self.assertEqual(by_ref["a-ssh"]["credential_model"], "coarse")
        self.assertEqual(by_ref["a-ssh"]["authority"], "proven")
        self.assertEqual(by_ref["a-ssh"]["lifecycle"], "ready")
        self.assertTrue(all(a["evidence"] == "coarse" for a in by_ref["a-ssh"]["abilities"]))
        # A scoped token nobody has asked about: reachable, with the debt named.
        self.assertEqual(by_ref["a-cloudflare_dns"]["credential_model"], "scoped")
        self.assertEqual(by_ref["a-cloudflare_dns"]["authority"], "undeclared")
        self.assertEqual(by_ref["a-cloudflare_dns"]["lifecycle"], "reachable")
        self.assertTrue(all(a["evidence"] == "unverified" for a in by_ref["a-cloudflare_dns"]["abilities"]))
        self.assertNotIn("token", by_ref["a-ssh"])

    def test_every_connection_provider_declares_a_credential_model(self):
        from control_plane.providers import (
            CONNECTION_CREDENTIALS,
            PROVIDERS,
            observer_abilities,
        )

        named = {p for spec in PROVIDERS.values() for p in spec.connection_providers}
        named |= {ability.provider for ability in observer_abilities()}
        self.assertTrue(named)
        self.assertEqual(sorted(named - set(CONNECTION_CREDENTIALS)), [])
        self.assertLessEqual(set(CONNECTION_CREDENTIALS.values()), {"scoped", "coarse", "none"})

    def test_keyless_gateways_prove_themselves_without_a_credential(self):
        lookup = Principal(
            "lookup", "test", frozenset({Capability.READ, Capability.LOOK_UP_PUBLIC_RECORDS})
        )
        outcome = list_connections(principal=lookup)
        registries = next(
            g for g in outcome["groups"] if g["name"] == "hq.public_registries"
        )
        for instance in registries["instances"]:
            self.assertEqual(instance["credential_model"], "none")
            self.assertEqual(instance["authority"], "proven")
            self.assertTrue(all(a["evidence"] == "not_applicable" for a in instance["abilities"]))
            self.assertTrue(all(a["grant"] == "none" for a in instance["abilities"]))
