"""What each credential can see: derived from the registries, read in one query."""

from __future__ import annotations

import io
import urllib.error
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderConnection, ProviderInventory
from control_plane.observations import OBSERVATIONS, ObservationRecord, ObservationSpec
from control_plane.observations.contract import registry
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
)
from control_plane.providers import CONNECTION_CREDENTIALS, CONNECTION_LABELS, PROVIDERS

from .connections import _controller_contract
from .credential_sight import (
    NEVER_SWEPT,
    NOT_CONNECTED,
    READABLE,
    REFUSED,
    UNREADABLE,
    credential_sight,
    sight_by_connection,
)
from .inventory import record_inventory
from .security import cli_principal


class ExampleRecord(ObservationRecord):
    name: str


EXAMPLE = ObservationSpec(
    "example.pages_project",
    "cloudflare_api",
    "Pages project",
    ExampleRecord,
    requires=("Pages Read (account)",),
)


def with_example():
    return mock.patch(
        "application.credential_sight.OBSERVATIONS",
        registry((*OBSERVATIONS.values(), EXAMPLE)),
    )


def sights_by_kind():
    return {
        (provider.provider, sight.kind): sight
        for provider in credential_sight()
        for sight in provider.sights
    }


def by_provider():
    return {provider.provider: provider for provider in credential_sight()}


def unobserved_kinds():
    return {kind for kind, spec in PROVIDERS.items() if spec.unobserved_reason}


class CredentialSightTests(TestCase):
    def test_every_swept_reading_and_resource_kind_of_a_credential_is_listed(self):
        found = sights_by_kind()

        for kind, spec in OBSERVATIONS.items():
            if spec.provider in CONNECTION_CREDENTIALS:
                self.assertIn((spec.provider, kind), found)
        for kind, spec in PROVIDERS.items():
            if spec.unobserved_reason:
                continue
            for provider in spec.connection_providers:
                self.assertIn((provider, kind), found)

    def test_a_kind_nothing_sweeps_is_not_listed(self):
        self.assertTrue(unobserved_kinds())
        listed = {kind for _, kind in sights_by_kind()}

        self.assertFalse(listed & unobserved_kinds())

    def test_no_kind_is_listed_twice(self):
        kinds = [kind for _, kind in sights_by_kind()]
        for provider in credential_sight():
            labels = [sight.label for sight in provider.sights]
            self.assertEqual(len(labels), len(set(labels)), provider.provider)
        self.assertTrue(kinds)

    def test_every_provider_with_a_credential_is_listed_with_its_label(self):
        providers = by_provider()

        self.assertEqual(set(providers), set(CONNECTION_CREDENTIALS))
        for name, provider in providers.items():
            self.assertEqual(provider.label, CONNECTION_LABELS[name])
        self.assertEqual(providers["adguard"].label, "AdGuard Home")
        self.assertEqual(providers["onepassword"].label, "1Password")

    def test_a_kind_with_no_inventory_row_was_never_swept(self):
        sight = sights_by_kind()[("ssh", "host.perimeter")]

        self.assertEqual(sight.state, NEVER_SWEPT)
        self.assertEqual(sight.source, "reading")

    def test_a_kind_with_no_connection_is_not_connected_rather_than_empty(self):
        ProviderInventory.objects.create(
            kind="host.perimeter", records=[], connected=False, observed_at=timezone.now()
        )

        sight = sights_by_kind()[("ssh", "host.perimeter")]

        self.assertEqual(sight.state, NOT_CONNECTED)
        self.assertEqual(sight.state_label, "Not connected")
        self.assertEqual(sight.remedy, "")

    def test_a_readable_kind_carries_its_age_and_record_count(self):
        observed = timezone.now()
        ProviderInventory.objects.create(
            kind="host.perimeter",
            records=[{"record": "a"}, {"record": "b"}],
            observed_at=observed,
        )

        sight = sights_by_kind()[("ssh", "host.perimeter")]

        self.assertEqual(sight.state, READABLE)
        self.assertEqual(sight.records, 2)
        self.assertEqual(sight.observed_at, observed)
        self.assertEqual(sight.remedy, "")

    def test_a_permission_refusal_names_the_permission_it_needs(self):
        ProviderInventory.objects.create(
            kind=EXAMPLE.kind,
            reachable=False,
            refusal=PERMISSION_REFUSAL,
            error="Cloudflare refused the request: Authentication error",
            observed_at=timezone.now(),
        )

        with with_example():
            sight = sights_by_kind()[("cloudflare_api", EXAMPLE.kind)]
            provider = by_provider()["cloudflare_api"]

        self.assertEqual(sight.state, REFUSED)
        self.assertEqual(sight.remedy, "Add Pages Read (account) to see Pages project")
        self.assertEqual(provider.credential_refusal, "")

    def test_a_refused_credential_is_said_once_and_offers_no_permission(self):
        for kind in (EXAMPLE.kind, "cloudflare.d1_database"):
            ProviderInventory.objects.create(
                kind=kind,
                reachable=False,
                refusal=CREDENTIAL_REFUSAL,
                error="Cannot use the access token from location: 192.0.2.1",
                observed_at=timezone.now(),
            )

        with with_example():
            provider = by_provider()["cloudflare_api"]
        refused = [sight for sight in provider.sights if sight.refusal]

        self.assertEqual(
            provider.credential_refusal,
            "Cannot use the access token from location: 192.0.2.1",
        )
        self.assertEqual(len(refused), 2)
        for sight in refused:
            self.assertEqual(sight.state, REFUSED)
            self.assertEqual(sight.remedy, "")
            self.assertEqual(sight.error, "")

    def test_a_failure_that_is_no_refusal_offers_no_permission(self):
        ProviderInventory.objects.create(
            kind=EXAMPLE.kind,
            reachable=False,
            error="Cloudflare request failed: TimeoutError.",
            observed_at=timezone.now(),
        )

        with with_example():
            sight = sights_by_kind()[("cloudflare_api", EXAMPLE.kind)]

        self.assertEqual(sight.state, UNREADABLE)
        self.assertEqual(sight.remedy, "")
        self.assertEqual(sight.error, "Cloudflare request failed: TimeoutError.")

    def test_every_inventory_row_is_read_in_one_query(self):
        now = timezone.now()
        for kind in list(PROVIDERS)[:5]:
            ProviderInventory.objects.create(kind=kind, records=[{}], observed_at=now)
        ProviderInventory.objects.create(
            kind="host.perimeter", reachable=False, error="refused", observed_at=now
        )

        with self.assertNumQueries(1):
            credential_sight()

    def test_providers_without_a_connection_are_split_out(self):
        connected, unconnected = sight_by_connection({"ssh"})

        self.assertEqual(set(connected), {"ssh"})
        self.assertEqual(
            {provider.provider for provider in unconnected},
            set(CONNECTION_CREDENTIALS) - {"ssh"},
        )

    def test_with_nothing_connected_no_provider_is_said_to_be_missing(self):
        self.assertEqual(sight_by_connection(set()), ({}, ()))


class CanDoTests(TestCase):
    def test_a_provider_can_do_every_reading_it_feeds(self):
        abilities, by_provider_names = _controller_contract()
        labels = {ability.name: ability.label for ability in abilities}

        for kind, spec in OBSERVATIONS.items():
            self.assertIn(kind, by_provider_names[spec.provider])
            self.assertEqual(labels[kind], spec.label)
        self.assertIn("analytics.read", by_provider_names["cloudflare_api"])

    def test_ability_names_are_unique(self):
        abilities, _ = _controller_contract()
        names = [ability.name for ability in abilities]

        self.assertEqual(len(names), len(set(names)))


def _http_error(code: int, error_code: int, message: str):
    body = (
        f'{{"success": false, "errors": [{{"code": {error_code}, '
        f'"message": "{message}"}}]}}'
    ).encode()
    return urllib.error.HTTPError("u", code, "Forbidden", {}, io.BytesIO(body))


class RefusalEndToEndTests(TestCase):
    """Controller report, then record_inventory, then what the page is given."""

    ENV = {
        "CF_CONNECTION_REF": "example-api",
        "CF_PROVIDER": "cloudflare_api",
        "CF_API_TOKEN": "t",
    }

    def _sweep(self, error):
        from controller_runtime import providers

        readers = {
            kind: reader
            for kind, reader in providers.OBSERVATION_READERS.items()
            if OBSERVATIONS[kind].provider == "cloudflare_api"
        }

        def refuse(*args, **kwargs):
            raise error()

        with (
            mock.patch.dict("os.environ", self.ENV, clear=True),
            mock.patch.dict(providers.PROVIDER_INVENTORY, readers, clear=True),
            mock.patch.object(providers.urllib.request, "urlopen", side_effect=refuse),
        ):
            report = providers.inventory()
        record_inventory(report, principal=cli_principal())
        return report, by_provider()["cloudflare_api"]

    def test_a_refused_credential_reaches_the_page_as_one_refusal(self):
        report, provider = self._sweep(
            lambda: _http_error(
                403, 9109, "Cannot use the access token from location: 192.0.2.1"
            )
        )

        self.assertTrue(report)
        for kind, found in report.items():
            self.assertEqual(found.get("refusal"), CREDENTIAL_REFUSAL, kind)
        self.assertEqual(
            provider.credential_refusal,
            "Cannot use the access token from location: 192.0.2.1",
        )
        readings = [sight for sight in provider.sights if sight.source == "reading"]
        self.assertTrue(readings)
        for sight in readings:
            self.assertEqual(sight.state, REFUSED)
            self.assertEqual(sight.remedy, "")

    def test_a_missing_permission_reaches_the_page_as_its_remedy(self):
        report, provider = self._sweep(
            lambda: _http_error(403, 10000, "Authentication error")
        )

        for kind, found in report.items():
            self.assertEqual(found.get("refusal"), PERMISSION_REFUSAL, kind)
        self.assertEqual(provider.credential_refusal, "")
        d1 = next(sight for sight in provider.sights if sight.kind == "cloudflare.d1_database")
        self.assertEqual(d1.remedy, "Add D1 Read (account) to see D1 database")

    def test_a_refusal_is_cleared_by_a_read_that_succeeds(self):
        record_inventory(
            {"host.perimeter": {"ok": False, "error": "x", "refusal": CREDENTIAL_REFUSAL}},
            principal=cli_principal(),
        )
        record_inventory(
            {"host.perimeter": {"ok": True, "records": []}}, principal=cli_principal()
        )

        self.assertEqual(ProviderInventory.objects.get(kind="host.perimeter").refusal, "")

    def test_an_unknown_refusal_is_stored_as_none(self):
        record_inventory(
            {"host.perimeter": {"ok": False, "error": "x", "refusal": "other"}},
            principal=cli_principal(),
        )

        self.assertEqual(ProviderInventory.objects.get(kind="host.perimeter").refusal, "")


class CredentialSightPageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(user)
        now = timezone.now()
        for ref, provider in (
            ("example-ssh", "ssh"),
            ("example-api", "cloudflare_api"),
            ("example-tailnet", "tailscale"),
        ):
            ProviderConnection.objects.create(
                connection_ref=ref,
                controller_id="example-controller",
                provider=provider,
                endpoint="",
                observed_at=now,
            )
        # Fully readable.
        for kind in ("host.perimeter", "caddy.route"):
            ProviderInventory.objects.create(
                kind=kind, records=[{"record": "a"}], observed_at=now
            )
        # The credential itself refused.
        for kind, spec in OBSERVATIONS.items():
            if spec.provider == "cloudflare_api":
                ProviderInventory.objects.create(
                    kind=kind,
                    reachable=False,
                    refusal=CREDENTIAL_REFUSAL,
                    error="Cannot use the access token from location: 192.0.2.1",
                    observed_at=now,
                )
        # One permission refused.
        ProviderInventory.objects.create(
            kind="tailscale.user",
            reachable=False,
            refusal=PERMISSION_REFUSAL,
            error="Tailscale refused the request.",
            observed_at=now,
        )
        # No connection.
        ProviderInventory.objects.create(
            kind="adguard.rewrite", records=[], connected=False, observed_at=now
        )

    def test_the_page_shows_each_connection_and_what_it_can_see(self):
        response = self.client.get(reverse("control_plane:connections"))
        body = response.content.decode()

        self.assertNotContains(response, "What each credential can see")
        self.assertContains(response, 'class="connection-sight"', count=3)
        self.assertContains(
            response,
            "Cloudflare API refused this credential: "
            "Cannot use the access token from location: 192.0.2.1",
            count=1,
        )
        self.assertNotIn("Add D1 Read (account)", body)
        self.assertContains(response, "Add users:read to see Tailnet user.")
        self.assertContains(response, "2 readable")
        # Not connected, once each, with what it would let HQ see.
        self.assertContains(response, "<strong>AdGuard Home</strong>", count=1)
        self.assertContains(response, "Would let HQ see: Internal DNS record")
        self.assertContains(response, "<strong>1Password</strong>", count=1)
        self.assertContains(response, "Would let HQ manage: Certificate target")
        # Nothing sweeps these, so no credential is said to see them.
        self.assertNotIn("Nothing to observe", body)
        self.assertNotIn("Onepassword", body)
        self.assertNotIn("Adguard", body)

    def test_can_do_lists_every_reading_of_the_provider(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "Site analytics")
        for spec in OBSERVATIONS.values():
            if spec.provider == "cloudflare_api":
                self.assertContains(response, f"<span>{spec.label}</span>")
