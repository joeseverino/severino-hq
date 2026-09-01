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

from core.models import AuditLog
from control_plane.models import ManagedResource, ProviderInventory
from control_plane.providers import PROVIDERS, validate_spec

from unittest import mock

from .inventory import (
    AdoptCommand,
    AdoptServiceCommand,
    adopt,
    adopt_discovered,
    adopt_service,
    inventory_state,
    unmanaged,
    unmanaged_services,
)
from .inventory import confirm_observed, record_inventory
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

    def test_an_unreachable_provider_does_not_erase_what_it_last_held(self):
        """"Could not ask" and "nothing there" are different facts.

        A controller run without one provider's credential reports it as
        unreachable and empty. Storing that as the truth deletes everything HQ
        knew about a host that never changed, and every page downstream then
        says its containers are gone.
        """
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE, ANOTHER]}),
            principal=cli_principal(),
        )
        first = ProviderInventory.objects.get(kind="adguard.rewrite").observed_at

        record_sweep(
            {"adguard.rewrite": {"ok": False, "records": [], "error": "no credential"}},
            principal=cli_principal(),
        )

        snapshot = ProviderInventory.objects.get(kind="adguard.rewrite")
        self.assertEqual(len(snapshot.records), 2)
        # And dated to when they were seen, not to the sweep that missed them,
        # so the surfaces that age this data tell the truth about it.
        self.assertEqual(snapshot.observed_at, first)

    def test_a_provider_first_seen_unreachable_is_still_recorded(self):
        record_sweep(
            {"adguard.rewrite": {"ok": False, "records": [], "error": "timed out"}},
            principal=cli_principal(),
        )

        self.assertEqual(ProviderInventory.objects.get(kind="adguard.rewrite").records, [])

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
        record_inventory(
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
        record_inventory(
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
        # Stored without adopting, because the tests below exercise the manual
        # button -- which only has anything to do when nothing took the record
        # first. A real sweep adopts, and the test for that says so itself.
        record_inventory(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )

    def test_a_swept_record_is_managed_without_being_opted_in(self):
        """If HQ can see it, HQ manages it.

        The page used to list what HQ had found and not taken, one row at a
        time, waiting to be clicked. The decision was made when the credential
        was added; asking again per record is a question whose answer is always
        yes, and a list of them is a chore standing in for a choice.
        """

        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )

        response = self.client.get(reverse("control_plane:services"))

        self.assertContains(response, "app.example.com")
        self.assertTrue(
            ManagedResource.objects.filter(kind="adguard.rewrite").exists()
        )

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
        record_inventory(
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
        """Both tables render the same columns, so a row lines up with a row."""
        from control_plane.providers import service_facets

        facets = unmanaged_services()[0].facets

        self.assertEqual(
            [facet_id for facet_id, _, _ in facets],
            [facet_id for facet_id, _ in service_facets()],
        )
        # A facet nothing supplies for *this* service renders empty rather than
        # being dropped, so the columns still line up.
        empty = dict((facet_id, value) for facet_id, _, value in facets)
        self.assertEqual(empty["certificate"], "")

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


class AdoptedIsObservedTests(TestCase):
    """Adoption is the one write that starts in sync, so it must say so.

    Everything else is born unobserved and waits for a controller to look --
    correct, because a typed declaration is a claim about a world nobody has
    checked. An adopted spec was read from the live record moments earlier.

    Left unmarked it stays "never reported" forever: nothing queues a reconcile
    for a resource that has not drifted, so the first look never comes, and
    every service assembled from it reads as incomplete while it is running.
    """

    def setUp(self):
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )
        adopt_discovered("adguard.rewrite", principal=cli_principal())
        self.resource = ManagedResource.objects.get(kind="adguard.rewrite")

    def test_it_is_not_waiting_to_be_looked_at(self):
        self.assertEqual(
            self.resource.observed_generation, self.resource.generation
        )

    def test_it_carries_when_it_was_seen(self):
        self.assertIsNotNone(self.resource.last_observed_at)

    def test_its_status_is_what_the_provider_was_holding(self):
        self.assertEqual(
            self.resource.status.get("domain"), A_REWRITE["domain"]
        )

    def test_it_reads_as_healthy_rather_than_merely_recorded(self):
        from application.infrastructure import resource_health

        self.assertEqual(resource_health(self.resource)["state"], "healthy")


class NothingWaitsToBeOptedInTests(TestCase):
    """If HQ can see it, HQ manages it.

    The estate arrived a click at a time: every rewrite, proxy host and
    container a credential could reach sat in a list of things to take on, and
    the answer was always yes. The decision was made when the credential was
    added.

    Nothing is exempt. A token that can edit a zone is the decision that HQ
    manages it, the same way a Portainer credential is the decision about the
    containers behind it.
    """

    def swept(self, **kinds):
        record_sweep(a_sweep(**kinds), principal=cli_principal())
        return set(
            ManagedResource.objects.values_list("kind", flat=True)
        )

    def test_a_rewrite_a_credential_reached_needs_no_click(self):
        self.assertIn("adguard.rewrite", self.swept(**{"adguard.rewrite": [A_REWRITE]}))

    def test_nothing_is_left_offering_itself_for_adoption(self):
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE, ANOTHER]}),
            principal=cli_principal(),
        )

        self.assertEqual([item.hostname for item in unmanaged()], [])

    def test_a_domain_the_credential_reaches_is_taken_on(self):
        """The last place still asking. Holding the token is the answer."""

        kinds = self.swept(
            **{"cloudflare.zone": [{"zone": "example.com", "connection_ref": "a-token"}]}
        )

        self.assertIn("cloudflare.zone", kinds)


class ASweepConfirmsWhatItFindsTests(TestCase):
    """A sweep is HQ going and looking, so it may write down what it saw.

    Only a reconcile ever did. Nothing queues a reconcile for a resource that
    has not drifted, so the first look never came and a declaration nothing had
    touched reported "never reported" forever -- with whole services reading as
    unverified while every part of them was running and had just been seen.
    """

    def setUp(self):
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )
        self.resource = ManagedResource.objects.get(kind="adguard.rewrite")

    def test_a_declaration_the_sweep_found_unchanged_is_observed(self):
        self.assertEqual(
            self.resource.observed_generation, self.resource.generation
        )

    def test_it_reads_as_healthy_rather_than_awaiting_a_first_check(self):
        from application.infrastructure import resource_health

        self.assertEqual(resource_health(self.resource)["state"], "healthy")

    def test_a_declaration_that_has_drifted_is_left_for_a_reconcile(self):
        """Calling drift "observed" hides the difference this model exists for."""

        self.resource.spec = {**self.resource.spec, "answer": "10.0.0.99"}
        self.resource.generation += 1
        self.resource.save(update_fields=["spec", "generation"])

        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal()
        )

        self.resource.refresh_from_db()
        self.assertNotEqual(
            self.resource.observed_generation, self.resource.generation
        )

    def test_a_provider_that_could_not_be_reached_confirms_nothing(self):
        before = ManagedResource.objects.get(kind="adguard.rewrite").last_observed_at

        record_sweep(
            {"adguard.rewrite": {"ok": False, "records": [], "error": "timed out"}},
            principal=cli_principal(),
        )

        self.assertEqual(
            ManagedResource.objects.get(kind="adguard.rewrite").last_observed_at, before
        )


def _adoptable():
    """Every provider a sweep can rebuild a spec for, with its sample record."""

    return [(kind, p) for kind, p in sorted(PROVIDERS.items())
            if p.from_record and p.sample_record]


class DriftIsSaidOutLoudTests(TestCase):
    """A sweep that finds a contradiction has to report one.

    Drift was skipped in silence: not confirmed, and not described. The
    declaration kept the condition from the last time it *did* match -- "the
    last sweep found this exactly as declared" -- beside a timestamp slowly
    ageing away from it, so the page read Healthy and In sync while the world
    said the opposite. Five tailnet devices sat like that for days.
    """

    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="a-device",
            kind="tailscale.device",
            spec={"connection_ref": "", "name": "a-box", "key_expiry_disabled": False},
        )
        self.resource.conditions = [
            {
                "type": "Ready",
                "status": True,
                "reason": "Observed",
                "message": "The last sweep found this exactly as declared.",
            }
        ]
        self.resource.save(update_fields=["conditions"])

    def _sweep(self, key_expiry_disabled):
        # A sweep reports the expiry date, and its *absence* is the setting --
        # see `_tailnet_device_from_record`. Built from the record shape rather
        # than from the spec shape, so this exercises the same mapping the real
        # sweep goes through.
        confirm_observed(
            {
                "tailscale.device": {
                    "ok": True,
                    "records": [
                        {
                            "name": "a-box",
                            "key_expires": (
                                "" if key_expiry_disabled else "2027-01-01T00:00:00Z"
                            ),
                            "tags": [],
                        }
                    ],
                }
            }
        )
        self.resource.refresh_from_db()

    def test_the_message_names_the_field_and_both_values(self):
        self._sweep(True)

        self.assertIn(
            "key_expiry_disabled is True, where this asks for False",
            self.resource.conditions[0]["message"],
        )

    def test_the_summary_card_reports_the_drift_too(self):
        """A condition nothing reads is a condition that was not written.

        Asserted through `resource_health`, because that is what the page shows
        above the conditions table -- and a false `Ready` is not the opposite of
        a true one, it is a row that surface skips entirely.
        """

        from application.infrastructure import resource_health

        self._sweep(True)

        self.assertEqual(resource_health(self.resource)["state"], "drifted")

    def test_a_matching_record_is_still_confirmed(self):
        self._sweep(False)

        self.assertIsNotNone(self.resource.last_observed_at)
        self.assertEqual(self.resource.conditions[0]["reason"], "Observed")


class UnobservableFieldTests(TestCase):
    """A blank the provider could never fill is not a disagreement.

    NPM answers with a certificate id, not an HQ resource key, so
    `_proxy_from_record` blanks `certificate_resource`. Compared as a value,
    every proxy host that named a certificate differed from its declaration
    forever -- and `confirm_observed` rightly refuses to call drift observed, so
    those resources kept the condition their last reconcile wrote and no sweep
    ever confirmed them again. They read healthy the whole time.
    """

    def setUp(self):
        self.principal = cli_principal()
        self.resource = ManagedResource.objects.create(
            key="secured-proxy", kind="npm.proxy_host",
            spec={**PROVIDERS["npm.proxy_host"].from_record(A_PROXY),
                  "certificate_resource": "example-wildcard"})

    def sweep(self, record=None):
        record_sweep(a_sweep(**{"npm.proxy_host": [record or A_PROXY]}),
                     principal=self.principal)
        self.resource.refresh_from_db()

    def test_a_host_naming_a_certificate_is_confirmed_by_a_sweep(self):
        self.assertIsNone(self.resource.last_observed_at)
        self.sweep()
        self.assertIsNotNone(self.resource.last_observed_at)
        self.assertEqual(self.resource.conditions[0]["reason"], "Observed")

    def test_the_certificate_a_sweep_cannot_see_is_never_erased(self):
        self.sweep()
        self.assertEqual(self.resource.spec["certificate_resource"], "example-wildcard")

    def test_drift_in_an_observable_field_is_still_refused(self):
        """The fix must not become a blanket exemption for the whole record."""
        self.sweep({**A_PROXY, "forward_port": 9999})
        self.assertIsNone(self.resource.last_observed_at)

    def test_every_unobservable_field_names_a_real_field_of_its_spec(self):
        for kind, provider in PROVIDERS.items():
            for field in provider.unobservable_fields:
                with self.subTest(kind=kind, field=field):
                    self.assertIn(field, provider.spec_type.model_fields)

    def test_every_adoptable_provider_supplies_a_record_to_check(self):
        """The guard must cover the registry, not a list kept by hand.

        This began as two kinds in a literal while eight providers could be
        adopted, so the bug it exists for was unguarded in six of them and
        nothing said so.
        """
        adoptable = {k for k, p in PROVIDERS.items() if p.from_record}
        sampled = {k for k, p in PROVIDERS.items() if p.sample_record}
        self.assertEqual(adoptable - sampled, set())

    def test_a_provider_blanking_a_field_it_declares_must_say_so(self):
        for kind, provider in _adoptable():
            rebuilt = provider.from_record(provider.sample_record)
            for field, value in rebuilt.items():
                if value != "" or field not in provider.spec_type.model_fields:
                    continue
                if provider.sample_record.get(field, "") == "":
                    continue  # the record itself was blank; nothing was invented
                with self.subTest(kind=kind, field=field):
                    self.assertIn(field, provider.unobservable_fields,
                        f"{kind}.{field} is blanked by from_record but not declared "
                        "unobservable, so a declaration naming it is never confirmed")

    def test_a_field_the_reading_never_carries_is_declared_even_when_omitted(self):
        """The guard above skips fields its sample record omits, which is
        exactly what a field the reading never carries looks like.

        Asked behaviourally instead: set the field, sweep the record it was
        built from, and nothing should be left unconfirmed.
        """

        from .topology import _unconfirmed

        for kind, provider in _adoptable():
            declared = set(provider.unobservable_fields or ())
            absent = set(provider.spec_type.model_fields) - set(provider.sample_record)
            for field in sorted(absent - declared):
                # Typed from the model, not the built spec: `from_record`
                # never emits these, so the result has no type to read.
                if provider.spec_type.model_fields[field].annotation is not str:
                    continue
                spec = provider.from_record(provider.sample_record)
                spec[field] = "held-by-hq"
                with self.subTest(kind=kind, field=field):
                    resource = ManagedResource.objects.create(
                        key=f"unconfirmed-{kind.replace('.', '-')}-{field}",
                        kind=kind,
                        spec=spec,
                    )
                    record_sweep(
                        a_sweep(**{kind: [provider.sample_record]}),
                        principal=cli_principal(),
                    )
                    resource.refresh_from_db()
                    self.assertEqual(
                        _unconfirmed(resource, provider), (),
                        f"{kind}.{field} is asserted by a declaration and never "
                        "read back, so it raises a finding no sweep or reconcile "
                        "can clear. Declare it in unobservable_fields.")

    def test_an_unobservable_field_that_does_round_trip_is_a_false_exemption(self):
        """Exemptions accrete, and each one forgives drift forever."""
        for kind, provider in _adoptable():
            rebuilt = provider.from_record(provider.sample_record)
            for field in provider.unobservable_fields:
                supplied = provider.sample_record.get(field, "")
                if supplied == "":
                    continue
                with self.subTest(kind=kind, field=field):
                    self.assertNotEqual(str(rebuilt.get(field, "")), str(supplied))

    def test_a_swept_record_confirms_the_declaration_it_matches(self):
        """The invariant, asked of every adoptable provider.

        This is the test that fails the day any provider acquires a blanked
        field, whether or not anyone thought about that field.
        """
        for kind, provider in _adoptable():
            with self.subTest(kind=kind):
                spec = provider.from_record(provider.sample_record)
                resource = ManagedResource.objects.create(
                    key=f"sample-{kind.replace('.', '-')}", kind=kind, spec=spec)
                record_sweep(a_sweep(**{kind: [provider.sample_record]}),
                             principal=cli_principal())
                resource.refresh_from_db()
                self.assertIsNotNone(resource.last_observed_at,
                    f"a {kind} declaration was not confirmed by a sweep of the "
                    "very record it was built from")
                self.assertEqual(resource.conditions[0]["reason"], "Observed")


class ObservationIsNotAnEventTests(TestCase):
    """A sweep confirming nothing changed must not write an audit row.

    `confirm_observed` stamps `last_observed_at` on every declaration it
    matches, every pass. Audited as a change that is one row per resource per
    sweep -- at a sixty-second interval, thousands a day saying "checked, still
    fine", with every real event buried among them.
    """

    def setUp(self):
        self.principal = cli_principal()
        self.resource = ManagedResource.objects.create(
            key="watched", kind="adguard.rewrite", spec=dict(A_REWRITE))
        AuditLog.objects.all().delete()

    def sweep(self, record=None):
        record_sweep(a_sweep(**{"adguard.rewrite": [record or A_REWRITE]}),
                     principal=self.principal)
        self.resource.refresh_from_db()

    def updates(self):
        return AuditLog.objects.filter(object_type="Managed resource",
                                       action=AuditLog.Action.UPDATED)

    def test_confirming_an_unchanged_declaration_writes_no_audit_row(self):
        # The first sweep is a real event: status, conditions and the observed
        # generation all move from empty to observed. Every sweep after it
        # changes only the stamp, and those are the thousands of rows.
        self.sweep()
        AuditLog.objects.all().delete()
        for _ in range(5):
            self.sweep()
        self.assertIsNotNone(self.resource.last_observed_at)
        self.assertEqual(self.updates().count(), 0)

    def test_the_stamp_is_still_written_so_staleness_still_works(self):
        """Silencing the event must not silence the fact it records."""
        self.sweep()
        first = self.resource.last_observed_at
        AuditLog.objects.all().delete()
        self.sweep()
        self.assertIsNotNone(first)
        self.assertGreaterEqual(self.resource.last_observed_at, first)

    def test_a_sweep_that_finds_something_different_is_still_an_event(self):
        self.sweep()
        AuditLog.objects.all().delete()
        self.sweep()
        self.assertEqual(self.updates().count(), 0, "a quiet sweep is not an event")
        ManagedResource.objects.filter(pk=self.resource.pk).update(
            spec={**A_REWRITE, "answer": "10.0.0.99"})
        self.sweep({**A_REWRITE, "answer": "10.0.0.99"})
        self.assertEqual(self.updates().count(), 1)
        changed = self.updates().first().metadata["changes"]
        # The stamp rides along with the real change rather than vanishing.
        self.assertIn("status", changed)
        self.assertIn("last_observed_at", changed)


class EveryKindIsWatchedOrSaysWhyNotTests(TestCase):
    """The collector registry and the provider list were never joined.

    One is a dict in the controller, the other is this list of kinds, and
    nothing compared them -- so a kind could be declared and swept by nothing
    at all, indefinitely, with the only symptom a staleness finding no sweep
    could ever clear.
    """

    def _collected(self):
        try:
            from controller_runtime.providers import PROVIDER_INVENTORY
        except Exception:  # pragma: no cover - controller extras absent
            self.skipTest("the controller runtime is not importable here")
        return set(PROVIDER_INVENTORY)

    def test_a_kind_nothing_collects_says_why(self):
        collected = self._collected()
        for kind, provider in sorted(PROVIDERS.items()):
            if kind in collected:
                continue
            with self.subTest(kind=kind):
                self.assertTrue(
                    provider.unobserved_reason,
                    f"nothing sweeps {kind!r} and its provider does not say why. "
                    "Either add a collector or state what cannot be reached.",
                )

    def test_a_kind_that_is_collected_does_not_claim_otherwise(self):
        """Exemptions rot: a collector arriving must retire the excuse."""

        for kind in sorted(self._collected()):
            provider = PROVIDERS.get(kind)
            if provider is None:
                continue
            with self.subTest(kind=kind):
                self.assertFalse(
                    provider.unobserved_reason,
                    f"{kind!r} is swept but still claims nothing observes it",
                )


class LineEndingsAreNotDriftTests(TestCase):
    """A document saved through a form is the same document the API returns.

    HTML submits a textarea as CRLF and every provider returns LF, so the two
    differ byte for byte while saying exactly the same thing. A tailnet policy
    sat "drifted" on that for a week, seconds after a reconcile that Tailscale
    accepted and that HQ recorded as "the policy is as declared".
    """

    DOCUMENT = '{\n  "grants": [],\n  "groups": {}\n}'

    def test_crlf_and_lf_are_the_same_declaration(self):
        from .inventory import _differences

        declared = {"document": self.DOCUMENT.replace("\n", "\r\n")}
        found = {"document": self.DOCUMENT}

        self.assertEqual(_differences("tailscale.policy", declared, found), ())

    def test_a_document_really_changing_is_still_drift(self):
        """The normalisation must not swallow a difference that matters."""

        from .inventory import _differences

        declared = {"document": self.DOCUMENT.replace("\n", "\r\n")}
        found = {"document": self.DOCUMENT.replace("grants", "grant")}

        self.assertNotEqual(_differences("tailscale.policy", declared, found), ())

    def test_a_spec_saved_with_crlf_is_stored_with_one_newline(self):
        """Fixed on the way in as well, so the stored value is comparable to
        anything, not only to what this one comparison normalises."""

        spec = validate_spec(
            "tailscale.policy",
            {"connection_ref": "", "document": self.DOCUMENT.replace("\n", "\r\n")},
        )

        self.assertNotIn("\r", spec["document"])
        self.assertEqual(spec["document"], self.DOCUMENT)


class NothingIsJudgedAgainstAReadingThatDoesNotExistTests(TestCase):
    """A spec and a reading are two vocabularies, and some never overlap.

    A certificate declares what was asked for -- which name, which domains,
    where to install -- and its reading reports what exists: issuer, expiry,
    the PEM. Compared by field name every declared field is unconfirmed
    forever, and no sweep or reconcile can clear it. The same was true of a
    machine, whose only reading is telemetry.

    The test is `from_record`: without one there is no way to turn a reading
    into a spec, so there is nothing to compare and nothing to report.
    """

    class _Resource:
        def __init__(self, spec, status):
            self.spec = spec
            self.status = status
            self.last_observed_at = timezone.now()

    def _unconfirmed(self, kind, status):
        from .topology import _unconfirmed

        provider = PROVIDERS[kind]
        spec = {field: "declared" for field in provider.spec_type.model_fields}
        return _unconfirmed(self._Resource(spec, status), provider)

    def test_a_provider_with_no_reading_reports_nothing_unconfirmed(self):
        for kind, provider in sorted(PROVIDERS.items()):
            if provider.from_record:
                continue
            with self.subTest(kind=kind):
                self.assertEqual(
                    self._unconfirmed(kind, {"something": "else"}), (),
                    f"{kind} has no from_record, so every declared field would "
                    "report unconfirmed forever with nothing able to clear it",
                )

    def test_a_provider_with_a_reading_is_still_judged(self):
        """The exemption must not quietly turn drift detection off."""

        unconfirmed = self._unconfirmed("tailscale.device", {"name": "declared"})
        self.assertTrue(
            unconfirmed,
            "a provider that can be read back must still report what its "
            "reading did not confirm",
        )
        self.assertNotIn("name", unconfirmed)


class ADeclarationCanAlwaysBeGotRidOfTests(TestCase):
    """Removing a declaration must not depend on HQ being able to delete the
    thing it describes.

    A tailnet device joined by somebody running `tailscale up` on it. HQ never
    created it and has no delete for it, so removal queued a controller action
    the provider does not implement and was refused -- leaving a declaration
    for a device that had been renamed away impossible to remove, and a finding
    about it that nothing could clear.
    """

    def test_removal_is_offered_for_everything_hq_did_not_create(self):
        from control_plane.models import OperationRequest
        from control_plane.providers import controller_action_policy

        for kind, provider in sorted(PROVIDERS.items()):
            if provider.declaration_only:
                continue  # removal forgets the declaration; always available
            allowed, explanation = controller_action_policy(
                kind, OperationRequest.Action.DELETE
            )
            with self.subTest(kind=kind):
                self.assertTrue(
                    allowed or provider.removal_gap,
                    f"{kind} is not declaration-only, so removal queues a "
                    f"controller delete -- which is refused: {explanation}. "
                    "Implement the delete, mark it declaration-only, or say in "
                    "`removal_gap` why its declarations cannot be removed.",
                )

    def test_a_stated_removal_gap_is_retired_once_removal_works(self):
        """Exemptions accrete, and each one hides a declaration nobody can
        get rid of."""

        from control_plane.models import OperationRequest
        from control_plane.providers import controller_action_policy

        for kind, provider in sorted(PROVIDERS.items()):
            if not provider.removal_gap:
                continue
            allowed, _ = controller_action_policy(
                kind, OperationRequest.Action.DELETE
            )
            with self.subTest(kind=kind):
                self.assertFalse(
                    allowed or provider.declaration_only,
                    f"{kind} can be removed now and should stop saying it cannot",
                )
