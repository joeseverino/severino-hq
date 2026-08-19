"""Seeing what the providers hold, and adopting it without changing it.

The safety property that makes one-click adoption defensible: the spec is read
back out of the live record, so the declaration starts equal to the world and
the first reconciliation is a no-op. If that ever stops being true, adopting a
proxy host silently resets it to HQ's defaults -- which is exactly the bug that
used to switch HSTS off.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderInventory
from control_plane.providers import PROVIDERS, validate_spec

from unittest import mock

from .inventory import (
    AdoptCommand,
    AdoptServiceCommand,
    adopt,
    adopt_service,
    inventory_state,
    unmanaged,
    unmanaged_services,
)
from .sweep import record_sweep
from .infrastructure import NotFoundError
from .security import cli_principal

A_REWRITE = {"domain": "app.example.com", "answer": "10.0.0.10", "enabled": True}
ANOTHER = {"domain": "tool.example.com", "answer": "10.0.0.11", "enabled": True}
A_PROXY = {
    "domain_names": ["shop.example.com"],
    "forward_scheme": "http",
    "forward_host": "10.0.0.20",
    "forward_port": 3000,
    "ssl_forced": True,
    "http2_support": True,
    "allow_websocket_upgrade": True,
    "caching_enabled": False,
    "block_exploits": True,
    "access_list_id": 0,
    "advanced_config": "",
    "hsts_enabled": True,
    "hsts_subdomains": True,
    "trust_forwarded_proto": True,
    "enabled": True,
}


def a_sweep(**kinds) -> dict:
    return {kind: {"ok": True, "records": records} for kind, records in kinds.items()}


class RecordingTests(TestCase):
    def test_a_sweep_replaces_the_last_one(self):
        """Merging would keep records the provider has since deleted.

        A cache that only ever grows reports things that are gone, which is the
        one failure a staleness-aware store must not have.
        """
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE, ANOTHER]}),
            principal=cli_principal(),
        )
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )

        self.assertEqual(
            len(ProviderInventory.objects.get(kind="adguard.rewrite").records), 1
        )

    def test_an_unreachable_provider_is_recorded_as_unreachable(self):
        record_sweep(
            {"adguard.rewrite": {"ok": False, "records": [], "error": "timed out"}},
            principal=cli_principal(),
        )

        snapshot = ProviderInventory.objects.get(kind="adguard.rewrite")
        self.assertFalse(snapshot.reachable)
        self.assertEqual(snapshot.error, "timed out")

    def test_a_kind_this_hq_does_not_know_is_ignored_not_rejected(self):
        """A controller ahead of this HQ must not take the whole sweep down."""
        result = record_sweep(
            {
                "invented.kind": {"ok": True, "records": [{}]},
                "adguard.rewrite": {"ok": True, "records": [A_REWRITE]},
            },
            principal=cli_principal(),
        )

        self.assertEqual(result["recorded"], ["adguard.rewrite"])

    def test_state_reports_what_each_provider_last_said(self):
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE, ANOTHER]}),
            principal=cli_principal(),
        )

        state = inventory_state()

        self.assertEqual(state[0]["count"], 2)
        self.assertEqual(state[0]["label"], "Internal DNS record")


class UnmanagedTests(TestCase):
    def setUp(self):
        record_sweep(
            a_sweep(
                **{
                    "adguard.rewrite": [A_REWRITE, ANOTHER],
                    "npm.proxy_host": [A_PROXY],
                }
            ),
            principal=cli_principal(),
        )

    def test_everything_is_unmanaged_until_something_declares_it(self):
        found = {(item.kind, item.hostname) for item in unmanaged()}

        self.assertEqual(
            found,
            {
                ("adguard.rewrite", "app.example.com"),
                ("adguard.rewrite", "tool.example.com"),
                ("npm.proxy_host", "shop.example.com"),
            },
        )

    def test_a_declaration_accounts_for_its_record(self):
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
        )

        self.assertNotIn(
            "app.example.com", [item.hostname for item in unmanaged()]
        )

    def test_matching_is_by_hostname_not_by_the_declared_value(self):
        """The reconcilers find their record by hostname, so this must too.

        A declaration whose answer has drifted from the live record is still the
        same record -- offering to adopt it would create a second declaration
        for one rewrite.
        """
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.9.9.9"},
        )

        self.assertNotIn(
            "app.example.com", [item.hostname for item in unmanaged()]
        )

    def test_a_disabled_declaration_does_not_account_for_a_live_record(self):
        """HQ is not managing it, and the record is still out there serving."""
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=False,
        )

        self.assertIn("app.example.com", [item.hostname for item in unmanaged()])


class AdoptionTests(TestCase):
    def setUp(self):
        record_sweep(
            a_sweep(**{"npm.proxy_host": [A_PROXY], "adguard.rewrite": [A_REWRITE]}),
            principal=cli_principal(),
        )

    def test_adopting_captures_the_record_exactly_as_it_is(self):
        """The whole safety argument: the first reconcile after this is a no-op.

        Every setting comes from the live record, including the ones HQ used to
        assert. Adopting with defaults instead would turn HSTS off on a host
        that had it on, at the next pass, with nobody having asked.
        """
        adopt(
            AdoptCommand(kind="npm.proxy_host", hostname="shop.example.com"),
            principal=cli_principal(),
        )

        spec = ManagedResource.objects.get(kind="npm.proxy_host").spec
        self.assertTrue(spec["hsts_enabled"])
        self.assertTrue(spec["hsts_subdomains"])
        self.assertTrue(spec["trust_forwarded_proto"])
        self.assertTrue(spec["websocket"])
        self.assertEqual(spec["forward_port"], 3000)

    def test_the_adopted_spec_is_one_the_model_accepts_unchanged(self):
        adopt(
            AdoptCommand(kind="npm.proxy_host", hostname="shop.example.com"),
            principal=cli_principal(),
        )

        spec = ManagedResource.objects.get(kind="npm.proxy_host").spec
        self.assertEqual(validate_spec("npm.proxy_host", spec), spec)

    def test_adopting_names_it_after_the_hostname(self):
        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        self.assertTrue(ManagedResource.objects.filter(key="app-example-com-dns").exists())

    def test_an_adopted_record_stops_being_offered(self):
        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        self.assertNotIn(
            "app.example.com", [item.hostname for item in unmanaged()]
        )

    def test_adopting_twice_is_refused_rather_than_duplicated(self):
        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        with self.assertRaises(NotFoundError):
            adopt(
                AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
                principal=cli_principal(),
            )

    def test_adopting_something_no_longer_there_says_so(self):
        with self.assertRaises(NotFoundError):
            adopt(
                AdoptCommand(kind="adguard.rewrite", hostname="ghost.example.com"),
                principal=cli_principal(),
            )

    def test_the_spec_is_read_from_the_record_not_from_the_request(self):
        """A browser could post a stale or edited copy; adoption captures truth."""
        ProviderInventory.objects.filter(kind="adguard.rewrite").update(
            records=[{"domain": "app.example.com", "answer": "10.0.0.55"}],
            observed_at=timezone.now(),
        )

        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        self.assertEqual(
            ManagedResource.objects.get(kind="adguard.rewrite").spec["answer"],
            "10.0.0.55",
        )


class AdoptionWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="test-only-password"
        )
        self.client.force_login(self.user)
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )

    def test_the_service_page_lists_what_hq_does_not_manage(self):
        response = self.client.get(reverse("control_plane:services"))

        self.assertContains(response, "Not managed by HQ")
        self.assertContains(response, "app.example.com")

    def test_adopting_from_the_page_creates_the_declaration(self):
        response = self.client.post(
            reverse("control_plane:adopt", kwargs={"hostname": "app.example.com"})
        )

        resource = ManagedResource.objects.get(kind="adguard.rewrite")
        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "app.example.com"}),
        )
        self.assertEqual(resource.spec["answer"], "10.0.0.10")

    def test_adopting_requires_a_signed_in_operator(self):
        self.client.logout()

        response = self.client.post(
            reverse("control_plane:adopt", kwargs={"hostname": "app.example.com"})
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertFalse(ManagedResource.objects.exists())


class ProviderRecordContractTests(TestCase):
    """Every provider that can be adopted must be able to read its own records."""

    def test_a_listable_provider_can_rebuild_a_spec_from_a_record(self):
        """Anything adoptable must say how to tell one of its records apart.

        This asserted ``hostnames``, which was the same thing while every
        provider held exactly one record per name. A zone holds several for one
        name and a domain declares no hostname at all, so the question the test
        was always asking -- "what makes this record itself" -- is now answered
        by ``identity``, falling back to the hostnames where they still say it.
        """

        for kind, provider in PROVIDERS.items():
            if provider.from_record is None:
                continue
            with self.subTest(kind=kind):
                self.assertTrue(
                    provider.identity is not None or provider.hostnames is not None,
                    f"{kind} can be adopted but has no identity to match on",
                )

    def test_a_rebuilt_spec_survives_its_own_model(self):
        for kind, record in (
            ("adguard.rewrite", A_REWRITE),
            ("npm.proxy_host", A_PROXY),
        ):
            with self.subTest(kind=kind):
                spec = PROVIDERS[kind].from_record(record)
                self.assertEqual(validate_spec(kind, spec), spec)


class AdoptServiceTests(TestCase):
    """A hostname is one decision, even when it is several records."""

    def setUp(self):
        record_sweep(
            a_sweep(
                **{
                    "adguard.rewrite": [
                        {"domain": "shop.example.com", "answer": "10.0.0.20"}
                    ],
                    "npm.proxy_host": [A_PROXY],
                }
            ),
            principal=cli_principal(),
        )

    def test_one_hostname_groups_its_records(self):
        services = unmanaged_services()

        self.assertEqual([s.hostname for s in services], ["shop.example.com"])
        self.assertEqual(len(services[0].items), 2)

    def test_the_grouped_columns_match_the_service_view(self):
        facets = unmanaged_services()[0].facets

        self.assertEqual([facet_id for facet_id, _, _ in facets], ["dns", "proxy", "certificate"])
        # A facet nothing supplies renders empty rather than being dropped, so
        # the columns line up with the managed table beside it.
        self.assertEqual(facets[2][2], "")

    def test_a_column_carries_the_value_and_not_its_label(self):
        """The heading already says DNS; "Answers with" in the cell is noise."""
        facets = dict((facet_id, value) for facet_id, _, value in unmanaged_services()[0].facets)

        self.assertEqual(facets["dns"], "10.0.0.20")
        self.assertEqual(facets["proxy"], "http://10.0.0.20:3000")

    def test_adopting_a_hostname_takes_everything_behind_it(self):
        adopt_service(
            AdoptServiceCommand(hostname="shop.example.com"),
            principal=cli_principal(),
        )

        self.assertEqual(ManagedResource.objects.count(), 2)
        self.assertEqual(unmanaged_services(), ())

    def test_a_hostname_nobody_saw_is_refused(self):
        with self.assertRaises(NotFoundError):
            adopt_service(
                AdoptServiceCommand(hostname="ghost.example.com"),
                principal=cli_principal(),
            )

    def test_a_failure_partway_adopts_nothing(self):
        """Half a service is worse than none: HQ would own the ingress and not
        the name, and the service page would show a gap that is not real."""
        ManagedResource.objects.create(
            key="shop-example-com-proxy", kind="adguard.rewrite", spec=A_REWRITE
        )

        with mock.patch(
            "application.inventory.adopt",
            side_effect=[{"resource": {"key": "one"}}, RuntimeError("boom")],
        ):
            with self.assertRaises(RuntimeError):
                adopt_service(
                    AdoptServiceCommand(hostname="shop.example.com"),
                    principal=cli_principal(),
                )

        self.assertEqual(ManagedResource.objects.count(), 1)
