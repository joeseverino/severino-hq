"""A credential's missing permissions, expiry and mint command, derived."""

from __future__ import annotations

import shlex
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.credential_reads import MINTERS, observer_permissions
from control_plane.models import ProviderConnection, ProviderInventory
from control_plane.observations import OBSERVATIONS
from control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL, PERMISSION_REFUSAL

from ..credential_mint import (
    credential_fixes,
    parse_expiry,
    projection_field,
    store_references,
)
from ..credential_sight import credential_sight
from ..findings import derive_findings
from ..inventory import record_connections
from ..security import cli_principal
from ..topology import derive_topology

ACCOUNT = "0123abcd"
STORE = {
    "vault": "Example Vault",
    "item": "exampleitem01",
    "bootstrap": "op://Operator Vault/Cloudflare bootstrap",
}
SECRET = "sekret-value-that-must-not-appear"
COMMAND = (
    "CLOUDFLARE_BOOTSTRAP_TOKEN='op://Operator Vault/Cloudflare bootstrap/credential' "
    f"op run -- ./scripts/mint-cloudflare-token.sh --account {ACCOUNT} "
    "--store 'op://Example Vault/exampleitem01/credential'"
)
PAGES = "cloudflare.pages_project"


def cloudflare_kinds() -> tuple[str, ...]:
    return tuple(kind for kind, spec in OBSERVATIONS.items() if spec.provider == "cloudflare_api")


def production_shape(refusal: str = CREDENTIAL_REFUSAL) -> None:
    """Pages read; every other account reading refused with code 10000.

    What an older controller stored: each refusal marked as the credential.
    """

    now = timezone.now()
    for kind in cloudflare_kinds():
        if kind == PAGES:
            ProviderInventory.objects.create(
                kind=kind,
                records=[{"name": "example", "account_id": ACCOUNT}],
                observed_at=now,
            )
        else:
            ProviderInventory.objects.create(
                kind=kind,
                reachable=False,
                refusal=refusal,
                error="Authentication error",
                observed_at=now,
            )


def connect(store=STORE, **fields) -> ProviderConnection:
    return ProviderConnection.objects.create(
        connection_ref="example-api",
        controller_id="example-controller",
        provider="cloudflare_api",
        store=store,
        observed_at=timezone.now(),
        **fields,
    )


def missing_from_readings() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                name
                for kind in cloudflare_kinds()
                if kind != PAGES
                for name in OBSERVATIONS[kind].requires
            }
        )
    )


class StoreReferenceTests(TestCase):
    def test_only_references_are_kept(self):
        self.assertEqual(
            store_references({**STORE, "API_TOKEN": SECRET, "value": SECRET}), STORE
        )

    def test_a_malformed_item_or_vault_is_dropped(self):
        self.assertEqual(store_references({"vault": "a/b", "item": "x"}), {})
        self.assertEqual(store_references({"vault": "Vault", "item": "has space"}), {})
        self.assertEqual(store_references("op://Vault/item"), {})

    def test_a_bootstrap_is_an_item_reference_outside_the_store_vault(self):
        for bootstrap in (
            "op://Example Vault/Cloudflare bootstrap",
            "op://Operator Vault/Cloudflare bootstrap/credential",
            "https://example.com",
            "op://Operator Vault/line\nbreak",
        ):
            with self.subTest(bootstrap=bootstrap):
                self.assertNotIn(
                    "bootstrap", store_references({**STORE, "bootstrap": bootstrap})
                )

    def test_expiry_is_an_aware_moment_or_nothing(self):
        self.assertIsNotNone(parse_expiry("2030-01-01T00:00:00Z"))
        self.assertIsNone(parse_expiry("2030-01-01T00:00:00"))
        self.assertIsNone(parse_expiry("soon"))
        self.assertIsNone(parse_expiry(None))

    def test_record_connections_keeps_references_and_expiry_and_no_secret(self):
        record_connections(
            [
                {
                    "connection_ref": "example-api",
                    "provider": "cloudflare_api",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "store": {**STORE, "API_TOKEN": SECRET},
                }
            ],
            principal=cli_principal(),
        )

        row = ProviderConnection.objects.get()
        self.assertEqual(row.store, STORE)
        self.assertEqual(row.expires_at.year, 2030)
        self.assertNotIn(SECRET, repr(row.store))


class MinterContractTests(TestCase):
    """Each minter names a script, flags and variables that script really has."""

    def test_every_minter_matches_its_script(self):
        for provider, minter in MINTERS.items():
            with self.subTest(provider=provider):
                script = (Path(settings.BASE_DIR) / minter.script).read_text()
                for flag, variable in minter.stores:
                    self.assertIn(flag, script)
                    projection_field(variable)
                for name, variable in minter.bootstrap:
                    self.assertIn(name, script)
                    projection_field(variable)
                if minter.account_flag:
                    self.assertIn(minter.account_flag, script)
                self.assertTrue(observer_permissions(provider))

    def test_store_fields_come_from_the_projection_registry(self):
        self.assertEqual(projection_field("API_TOKEN"), "credential")
        self.assertEqual(projection_field("CLIENT_SECRET"), "credential")
        self.assertEqual(projection_field("CLIENT_ID"), "username")


class PerReadingRefusalTests(TestCase):
    """The production case: one reading answers, the rest are refused."""

    def test_a_credential_that_read_something_is_not_refused_outright(self):
        production_shape()

        provider = next(found for found in credential_sight() if found.provider == "cloudflare_api")

        self.assertEqual(provider.credential_refusal, "")
        self.assertEqual(provider.missing, missing_from_readings())
        for sight in provider.refused_permission:
            self.assertEqual(sight.refusal, PERMISSION_REFUSAL)

    def test_a_credential_that_read_nothing_is_refused_outright(self):
        now = timezone.now()
        for kind in cloudflare_kinds():
            ProviderInventory.objects.create(
                kind=kind,
                reachable=False,
                refusal=CREDENTIAL_REFUSAL,
                error="Invalid API Token",
                observed_at=now,
            )

        provider = next(found for found in credential_sight() if found.provider == "cloudflare_api")

        self.assertEqual(provider.credential_refusal, "Invalid API Token")
        self.assertEqual(provider.missing, ())


class MintCommandTests(TestCase):
    def test_the_command_is_derived_from_references_only(self):
        production_shape()
        connect()

        fix = credential_fixes()["example-api"]

        self.assertEqual(fix.command, COMMAND)
        self.assertEqual(fix.gaps, ())
        self.assertEqual(fix.permissions, observer_permissions("cloudflare_api"))
        # Every word is a flag, the script, the account, or a reference.
        words = shlex.split(fix.command)
        for word in words:
            self.assertRegex(
                word,
                r"^(--[a-z-]+|\./scripts/mint-[a-z-]+\.sh|op|run|--|"
                rf"{ACCOUNT}|op://[^\n]+|[A-Z_]+=op://[^\n]+)$",
            )

    def test_no_item_reported_leaves_no_command(self):
        production_shape()
        connect(store={})

        fix = credential_fixes()["example-api"]

        self.assertEqual(fix.command, "")
        self.assertTrue(any("1Password item" in gap for gap in fix.gaps))

    def test_no_account_in_the_readings_leaves_no_command(self):
        production_shape()
        ProviderInventory.objects.filter(kind=PAGES).update(records=[{"name": "example"}])
        connect()

        fix = credential_fixes()["example-api"]

        self.assertEqual(fix.command, "")
        self.assertIn("No reading names the account yet.", fix.gaps)

    def test_no_bootstrap_item_reads_the_bootstrap_from_the_environment(self):
        production_shape()
        connect(store={"vault": "Example Vault", "item": "exampleitem01"})

        fix = credential_fixes()["example-api"]

        self.assertTrue(fix.command.startswith("./scripts/mint-cloudflare-token.sh "))
        self.assertTrue(any("CLOUDFLARE_BOOTSTRAP_TOKEN" in gap for gap in fix.gaps))

    def test_a_healthy_credential_offers_nothing(self):
        ProviderInventory.objects.create(
            kind=PAGES, records=[{"account_id": ACCOUNT}], observed_at=timezone.now()
        )
        connect(expires_at=timezone.now() + timedelta(days=80))

        fix = credential_fixes()["example-api"]

        self.assertFalse(fix.needed)
        self.assertEqual(fix.command, "")

    def test_the_tailscale_client_stores_its_secret_and_id(self):
        ProviderInventory.objects.create(
            kind="tailscale.user",
            reachable=False,
            refusal=PERMISSION_REFUSAL,
            error="Tailscale refused the users read (404).",
            observed_at=timezone.now(),
        )
        ProviderConnection.objects.create(
            connection_ref="example-tailnet",
            controller_id="example-controller",
            provider="tailscale",
            store={**STORE, "bootstrap": "op://Operator Vault/Tailscale bootstrap"},
            observed_at=timezone.now(),
        )

        fix = credential_fixes()["example-tailnet"]

        self.assertEqual(fix.missing, ("users:read",))
        self.assertEqual(
            fix.command,
            "TAILSCALE_BOOTSTRAP_CLIENT_ID='op://Operator Vault/Tailscale bootstrap/username' "
            "TAILSCALE_BOOTSTRAP_CLIENT_SECRET='op://Operator Vault/Tailscale bootstrap/credential' "
            "op run -- ./scripts/mint-tailscale-client.sh "
            "--store 'op://Example Vault/exampleitem01/credential' "
            "--store-id 'op://Example Vault/exampleitem01/username'",
        )


class CredentialFindingTests(TestCase):
    def findings(self):
        principal = cli_principal()
        return derive_findings(derive_topology(principal=principal), principal=principal)

    def test_missing_permissions_are_named_and_offer_the_mint(self):
        production_shape()
        connect()

        found = {finding.rule: finding for finding in self.findings()}

        self.assertNotIn("connection-not-answering", found)
        finding = found["credential-missing-permissions"]
        self.assertEqual(
            tuple(value for label, value in finding.evidence if label == "Missing"),
            missing_from_readings(),
        )
        self.assertEqual(finding.steps[0].command, COMMAND)

    def test_a_credential_refused_outright_offers_the_same_mint(self):
        now = timezone.now()
        ProviderInventory.objects.create(
            kind=PAGES, records=[{"account_id": ACCOUNT}], observed_at=now - timedelta(days=1)
        )
        ProviderInventory.objects.filter(kind=PAGES).update(
            reachable=False, refusal=CREDENTIAL_REFUSAL, error="Invalid API Token"
        )
        connect()

        found = {finding.rule: finding for finding in self.findings()}

        finding = found["connection-not-answering"]
        self.assertIn("refused", finding.title)
        self.assertNotIn("credential-missing-permissions", found)
        self.assertTrue(finding.steps)

    def test_expiry_inside_the_window_raises_a_finding_with_the_mint(self):
        ProviderInventory.objects.create(
            kind=PAGES, records=[{"account_id": ACCOUNT}], observed_at=timezone.now()
        )
        connect(expires_at=timezone.now() + timedelta(days=10))

        found = {finding.rule: finding for finding in self.findings()}

        finding = found["credential-expiring"]
        self.assertEqual(finding.severity, "attention")
        self.assertEqual(finding.steps[0].command, COMMAND)

    def test_an_expired_credential_is_serious(self):
        ProviderInventory.objects.create(
            kind=PAGES, records=[{"account_id": ACCOUNT}], observed_at=timezone.now()
        )
        connect(expires_at=timezone.now() - timedelta(days=1))

        finding = {f.rule: f for f in self.findings()}["credential-expiring"]

        self.assertEqual(finding.severity, "serious")
        self.assertIn("has expired", finding.title)

    def test_expiry_outside_the_window_is_quiet(self):
        ProviderInventory.objects.create(
            kind=PAGES, records=[{"account_id": ACCOUNT}], observed_at=timezone.now()
        )
        connect(expires_at=timezone.now() + timedelta(days=60))

        self.assertNotIn("credential-expiring", {f.rule for f in self.findings()})


class RenderedFixTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(user)
        production_shape()
        record_connections(
            [
                {
                    "connection_ref": "example-api",
                    "provider": "cloudflare_api",
                    "store": {**STORE, "API_TOKEN": SECRET},
                    "expires_at": (timezone.now() + timedelta(days=5)).isoformat(),
                }
            ],
            principal=cli_principal(),
            controller_id="example-controller",
        )

    def test_the_connections_page_offers_the_command_and_no_secret(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "operator-command")
        self.assertContains(response, "--account 0123abcd")
        self.assertContains(response, "op://Example Vault/exampleitem01/credential")
        self.assertNotContains(response, "Cloudflare API refused this credential")
        self.assertNotContains(response, SECRET)

    def test_the_findings_page_offers_the_command_and_no_secret(self):
        response = self.client.get(reverse("control_plane:findings"))

        self.assertContains(response, "operator-command")
        self.assertContains(response, "op://Example Vault/exampleitem01/credential")
        self.assertNotContains(response, SECRET)

    def test_the_findings_api_carries_the_step(self):
        from ..findings import findings

        found = findings(principal=cli_principal(), rule="credential-missing-permissions")
        steps = found["findings"][0]["operator_steps"]

        self.assertIn("--store", steps[0]["command"])
        self.assertNotIn(SECRET, repr(found))
