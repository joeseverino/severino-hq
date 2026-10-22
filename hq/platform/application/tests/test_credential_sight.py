"""What each credential can see: derived from the registries, at a cost that does not grow."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from hq.domains.control_plane.connection_kinds import CONNECTION_CREDENTIALS, CONNECTION_KINDS, CONNECTION_LABELS
from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS, ObservationRecord, ObservationSpec
from hq.domains.control_plane.observations.contract import registry
from hq.domains.control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
)
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.application.tests.markup import text

from ..connections import _controller_contract
from ..credential_sight import (
    NEVER_SWEPT,
    NOT_CONNECTED,
    READABLE,
    REFUSED,
    UNREADABLE,
    credential_sight,
    sight_by_connection,
)
from ..inventory import record_inventory
from ..security import cli_principal


def _cost(call) -> int:
    with CaptureQueriesContext(connection) as queries:
        call()
    return len(queries)


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
        "hq.platform.application.credential_sight.OBSERVATIONS",
        registry((*OBSERVATIONS.values(), EXAMPLE)),
    )


def sights_by_kind():
    return {(provider.provider, sight.kind): sight for provider in credential_sight() for sight in provider.sights}


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

    def test_a_kind_with_no_inventory_row_was_never_swept(self):
        sight = sights_by_kind()[("ssh", "host.perimeter")]

        self.assertEqual(sight.state, NEVER_SWEPT)
        self.assertEqual(sight.source, "reading")

    def test_a_kind_with_no_connection_is_not_connected_rather_than_empty(self):
        ProviderInventory.objects.create(kind="host.perimeter", records=[], connected=False, observed_at=timezone.now())

        sight = sights_by_kind()[("ssh", "host.perimeter")]

        self.assertEqual(sight.state, NOT_CONNECTED)
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
        self.assertIn(EXAMPLE.requires[0], sight.remedy)
        self.assertIn(EXAMPLE.label, sight.remedy)
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

        self.assertEqual(provider.credential_refusal, "Cannot use the access token from location: 192.0.2.1")
        self.assertEqual({sight.kind for sight in refused}, {EXAMPLE.kind, "cloudflare.d1_database"})
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

    def test_more_inventory_rows_cost_no_more_queries(self):
        now = timezone.now()
        ProviderInventory.objects.create(kind="host.perimeter", reachable=False, error="refused", observed_at=now)
        one_row = _cost(credential_sight)
        for kind in list(PROVIDERS)[:5]:
            ProviderInventory.objects.create(kind=kind, records=[{}], observed_at=now)

        self.assertEqual(_cost(credential_sight), one_row)

    def test_providers_without_a_connection_are_split_out(self):
        connected, unconnected = sight_by_connection({"edge": "ssh"})

        self.assertEqual(set(connected), {"edge"})
        self.assertEqual(connected["edge"].provider, "ssh")
        self.assertEqual(
            {provider.provider for provider in unconnected},
            set(CONNECTION_CREDENTIALS) - {"ssh"},
        )

    def test_with_nothing_connected_no_provider_is_said_to_be_missing(self):
        self.assertEqual(sight_by_connection({}), ({}, ()))


class PerConnectionSightTests(TestCase):
    """Two connections of one provider never show each other's readings."""

    def setUp(self):
        now = timezone.now()
        ProviderInventory.objects.create(
            kind="host.perimeter",
            records=[{"record": "perimeter", "connection_ref": "edge"}],
            observed_at=now,
        )
        ProviderInventory.objects.create(
            kind="caddy.route",
            records=[
                {"connection_ref": "edge", "domain": name, "upstream": "app:80"}
                for name in ("a.example.com", "b.example.com", "c.example.com")
            ],
            observed_at=now,
        )

    def labels(self, found):
        return {sight.kind: sight.records for sight in found.sights}

    def test_each_connection_counts_only_what_was_read_through_it(self):
        connected, _ = sight_by_connection({"edge": "ssh", "shared-host": "ssh"})

        self.assertEqual(self.labels(connected["edge"]), {"host.perimeter": 1, "caddy.route": 3})
        seen = self.labels(connected["shared-host"])
        self.assertNotIn("host.perimeter", seen)
        self.assertNotIn("caddy.route", seen)

    def test_the_provider_view_still_counts_every_connection(self):
        found = sights_by_kind()

        self.assertEqual(found[("ssh", "caddy.route")].records, 3)

    def test_a_failed_read_is_said_on_every_connection_it_could_be(self):
        ProviderInventory.objects.filter(kind="host.perimeter").update(reachable=False, error="exit 255")

        connected, _ = sight_by_connection({"edge": "ssh", "shared-host": "ssh"})

        for ref in ("edge", "shared-host"):
            states = {sight.kind: sight.state for sight in connected[ref].sights}
            self.assertEqual(states["host.perimeter"], UNREADABLE)

    def test_more_connections_cost_no_more_queries(self):
        one = _cost(lambda: sight_by_connection({"edge": "ssh"}))

        self.assertEqual(
            _cost(lambda: sight_by_connection({"edge": "ssh", "shared-host": "ssh", "dns": "cloudflare_dns"})),
            one,
        )

    def test_a_per_connection_kind_that_names_no_connection_is_refused(self):
        from hq.domains.control_plane.attribution import (
            PER_CONNECTION_PROVIDERS,
            unattributed_kinds,
        )

        class Unnamed(ObservationRecord):
            record: str

        class Named(ObservationRecord):
            connection_ref: str

        (provider,) = sorted(PER_CONNECTION_PROVIDERS)
        observations = registry(
            (
                ObservationSpec("example.unnamed", provider, "Unnamed", Unnamed),
                ObservationSpec("example.named", provider, "Named", Named),
                ObservationSpec("example.other", "tailscale", "Other", Unnamed),
            )
        )

        self.assertEqual(unattributed_kinds(observations, ()), ["example.unnamed"])
        self.assertEqual(unattributed_kinds(OBSERVATIONS, tuple(PROVIDERS.values())), [])


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


class RefusalEndToEndTests(TestCase):
    """Controller report, then record_inventory, then what the page is given."""

    def _sweep(self, refusal: str, error: str):
        report = {
            kind: {"ok": False, "records": [], "error": error, "refusal": refusal}
            for kind, spec in OBSERVATIONS.items()
            if spec.provider == "cloudflare_api" and spec.read_by == "controller"
        }
        record_inventory(report, principal=cli_principal())
        return report, by_provider()["cloudflare_api"]

    def test_a_refused_credential_reaches_the_page_as_one_refusal(self):
        report, provider = self._sweep(CREDENTIAL_REFUSAL, "Cannot use the access token from location: 192.0.2.1")

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
        report, provider = self._sweep(PERMISSION_REFUSAL, "Authentication error")

        for kind, found in report.items():
            self.assertEqual(found.get("refusal"), PERMISSION_REFUSAL, kind)
        self.assertEqual(provider.credential_refusal, "")
        d1 = next(sight for sight in provider.sights if sight.kind == "cloudflare.d1_database")
        spec = OBSERVATIONS["cloudflare.d1_database"]
        self.assertTrue(spec.requires)
        self.assertIn(spec.requires[0], d1.remedy)
        self.assertIn(spec.label, d1.remedy)

    def test_a_refusal_is_cleared_by_a_read_that_succeeds(self):
        record_inventory(
            {"host.perimeter": {"ok": False, "records": [], "error": "x", "refusal": CREDENTIAL_REFUSAL}},
            principal=cli_principal(),
        )
        record_inventory({"host.perimeter": {"ok": True, "records": []}}, principal=cli_principal())

        self.assertEqual(ProviderInventory.objects.get(kind="host.perimeter").refusal, "")

    def test_the_contract_allows_exactly_the_refusals_stored(self):
        from hq.domains.control_plane.bridge_contract import contract
        from hq.domains.control_plane.provider_adapters.contracts import FAILURES, REFUSALS

        schemas = contract()["components"]["schemas"]
        self.assertEqual(schemas["Refusal"]["enum"], ["", *REFUSALS])
        self.assertEqual(schemas["FailureClass"]["enum"], ["", *FAILURES])


class CredentialSightPageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="operator", password="not-a-real-password")
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
            ProviderInventory.objects.create(kind=kind, records=[{"record": "a"}], observed_at=now)
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
        ProviderInventory.objects.create(kind="adguard.rewrite", records=[], connected=False, observed_at=now)

    def rows(self, response):
        return {
            row.instance.connection_ref: row
            for _purpose, rows in response.context["connections"].purposes
            for row in rows
        }

    def test_the_page_shows_each_connection_and_what_it_can_see(self):
        response = self.client.get(reverse("control_plane:connections"))
        rows = self.rows(response)

        self.assertEqual(
            {ref: row.sight.provider for ref, row in rows.items() if row.sight},
            {"example-ssh": "ssh", "example-api": "cloudflare_api", "example-tailnet": "tailscale"},
        )
        # The refusal is the product's words: said once, for the credential.
        self.assertContains(
            response,
            "Cloudflare API refused this credential: Cannot use the access token from location: 192.0.2.1",
            count=1,
        )
        api = rows["example-api"].sight
        self.assertFalse([sight.remedy for sight in api.sights if sight.remedy])
        (tailnet_user,) = [sight for sight in rows["example-tailnet"].sight.sights if sight.kind == "tailscale.user"]
        self.assertIn("users:read", tailnet_user.remedy)
        said = text(response)
        self.assertIn(tailnet_user.remedy, said)
        readable = {sight.kind for sight in rows["example-ssh"].sight.sights if sight.state == READABLE}
        self.assertEqual(readable, {"host.perimeter", "caddy.route"})
        # Not connected: in the setup list, with what it would let HQ see.
        for provider in ("adguard", "onepassword"):
            self.assertIn(CONNECTION_LABELS[provider], said)
            self.assertIn(CONNECTION_KINDS[provider].gives, said)
        # A label is the provider's own name, never one made from its id.
        self.assertNotRegex(response.content.decode(), r"\b(Onepassword|Adguard)\b")

    def test_a_second_ssh_connection_does_not_show_the_first_ones_readings(self):
        ProviderConnection.objects.create(
            connection_ref="example-shared-host",
            controller_id="example-controller",
            provider="ssh",
            endpoint="",
            observed_at=timezone.now(),
        )
        ProviderInventory.objects.filter(kind="caddy.route").update(
            records=[{"connection_ref": "example-ssh", "domain": f"{n}.example.com"} for n in ("a", "b", "c")]
        )
        ProviderInventory.objects.filter(kind="host.perimeter").update(
            records=[{"record": "perimeter", "connection_ref": "example-ssh"}]
        )

        rows = self.rows(self.client.get(reverse("control_plane:connections")))

        def counted(ref):
            return {sight.kind: sight.records for sight in rows[ref].sight.sights if sight.records}

        self.assertEqual(counted("example-ssh"), {"host.perimeter": 1, "caddy.route": 3})
        self.assertEqual(counted("example-shared-host"), {})

    def test_can_do_lists_every_reading_of_the_provider(self):
        abilities, by_provider_names = _controller_contract()
        labels = {ability.name: ability.label for ability in abilities}

        response = self.client.get(reverse("control_plane:connections"))

        for name in by_provider_names["cloudflare_api"]:
            self.assertContains(response, escape(labels[name]))


class PartReasonTests(SimpleTestCase):
    def test_refusals_with_one_reason_are_one_line_naming_each_part_and_where(self):
        from hq.domains.control_plane.observations import ReadingPart
        from hq.domains.control_plane.reading_parts import PartRefusal

        from ..credential_sight import PARTIAL, Sight

        off = "not available on this repository (its plan or settings leave it off)."
        scanning, rules = ReadingPart("scanning", "Code scanning alerts"), ReadingPart("rules", "Branch rules")
        parts = (
            *(
                PartRefusal(kind="example", part=part, refusal="", reason=off, scope=f"example/{repository}")
                for repository in ("app", "site")
                for part in (scanning, rules)
            ),
            PartRefusal(kind="example", part=rules, refusal="", reason="GitHub timed out", scope="example/app"),
            PartRefusal(
                kind="example", part=scanning, refusal="", scope="example/web",
                reason="GitHub 404 for /repos/example/web/code-scanning/alerts?state=open",
            ),
            PartRefusal(
                kind="example", part=rules, refusal="", scope="example/web",
                reason="GitHub 404 for /repos/example/web/rules/branches/main",
            ),
        )  # fmt: skip

        sight = Sight(kind="example", label="Repository", source="reading", state=PARTIAL, parts=parts)

        self.assertEqual(
            sight.part_reasons,
            (
                "Code scanning alerts, Branch rules not read on app, site: "
                "not available on this repository (its plan or settings leave it off)",
                "Branch rules not read on app: GitHub timed out",
                "Code scanning alerts, Branch rules not read on web: hidden from HQ's connection",
            ),
        )

    def test_a_bare_status_is_said_as_what_it_means_to_the_connection_that_read(self):
        from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
        from hq.domains.control_plane.observations import ReadingPart
        from hq.domains.control_plane.reading_parts import PartRefusal

        provider, label = next(iter(CONNECTION_LABELS.items()))
        part = ReadingPart("rules", "Branch rules", provider=provider)

        def said(reason: str) -> str:
            return PartRefusal(kind="example", part=part, refusal="", reason=reason, scope="example/app").said

        self.assertEqual(said("provider answered 404"), f"hidden from HQ's {label}")
        self.assertEqual(said("Example 403 for /repos/example/app/rules"), f"closed to HQ's {label}")
        self.assertEqual(said("provider answered 503"), "the service failed to answer; the next read tries again")
        for reason in ("Example timed out", "Example listed more than 10 pages"):
            with self.subTest(reason):
                self.assertEqual(said(reason), reason)
