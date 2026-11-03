"""Seeing what the providers hold: what a sweep records, what a reading stands for, and what an observation is."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.providers import PROVIDERS, validate_spec
from hq.platform.core.models import AuditLog
from hq.platform.core.tests import audit_rows

from ..adoption import unmanaged
from ..inventory import inventory_state, record_inventory
from ..security import cli_principal
from ..sweep import record_sweep
from .inventory_records import A_PROXY, A_REWRITE, ANOTHER, a_sweep


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
        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())

        self.assertEqual(len(ProviderInventory.objects.get(kind="adguard.rewrite").records), 1)

    def test_a_provider_first_seen_unreachable_is_recorded_as_unreachable(self):
        record_sweep(
            {"adguard.rewrite": {"ok": False, "records": [], "error": "timed out"}},
            principal=cli_principal(),
        )

        snapshot = ProviderInventory.objects.get(kind="adguard.rewrite")
        self.assertFalse(snapshot.reachable)
        self.assertEqual(snapshot.error, "timed out")
        self.assertEqual(snapshot.records, [])

    def test_an_unreachable_provider_does_not_erase_what_it_last_held(self):
        """ "Could not ask" and "nothing there" are different facts.

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


class ReadingStandingTests(TestCase):
    """Each provider reading worded from one vocabulary: credential sight's
    states, and freshness's once a readable one is out of date."""

    def row(self, kind, **fields):
        ProviderInventory.objects.create(kind=kind, records=[A_REWRITE], observed_at=timezone.now(), **fields)

    def states(self):
        return {item["kind"]: item["state_label"] for item in inventory_state()}

    def test_refused_unreadable_stale_and_current_read_differently(self):
        from datetime import timedelta

        from hq.domains.control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL

        self.row("adguard.rewrite")
        self.row("npm.proxy_host", reachable=False, error="The address answered with a web page.")
        self.row("cloudflare.access_app", reachable=False, refusal=CREDENTIAL_REFUSAL, error="Invalid API Token")
        self.row("caddy.route")
        ProviderInventory.objects.filter(kind="caddy.route").update(observed_at=timezone.now() - timedelta(days=30))

        self.assertEqual(
            self.states(),
            {
                "adguard.rewrite": "Readable",
                "npm.proxy_host": "Could not be read",
                "cloudflare.access_app": "Refused",
                "caddy.route": "Out of date",
            },
        )

    def test_the_page_names_the_state_never_a_generic_unreachable(self):
        self.row("npm.proxy_host", reachable=False, error="The address answered with a web page.")
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:services"))

        self.assertRegex(
            response.content.decode(),
            r'title="The address answered with a web page\."\s*>\s*Could not be read<',
        )
        self.assertNotContains(response, ">unreachable<")

    def test_a_kind_no_connection_reads_is_not_listed(self):
        self.row("npm.proxy_host", connected=False, reachable=False)

        self.assertNotIn("npm.proxy_host", self.states())


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

    def test_a_declaration_accounts_for_its_record_by_hostname_not_by_value(self):
        """The reconcilers find their record by hostname, so this must too.

        A declaration whose answer has drifted from the live record is still the
        same record: offering to adopt it would create a second declaration
        for one rewrite.
        """
        for answer in ("10.0.0.10", "10.9.9.9"):
            with self.subTest(answer=answer):
                resource = ManagedResource.objects.create(
                    key="app-dns",
                    kind="adguard.rewrite",
                    spec={"domain": "app.example.com", "answer": answer},
                )

                self.assertNotIn("app.example.com", [item.hostname for item in unmanaged()])
                resource.delete()

    def test_a_disabled_declaration_does_not_account_for_a_live_record(self):
        """HQ is not managing it, and the record is still out there serving."""
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=False,
        )

        self.assertIn("app.example.com", [item.hostname for item in unmanaged()])


class ProviderRecordContractTests(TestCase):
    """Every provider that can be adopted must be able to read its own records."""

    def test_a_listable_provider_can_rebuild_a_spec_from_a_record(self):
        """Anything adoptable must say how to tell one of its records apart.

        ``hostnames`` is not enough: a zone holds several records for one name
        and a domain declares no hostname at all. ``identity`` answers "what
        makes this record itself", falling back to the hostnames where they say
        it.
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


class ObservationIsNotAnEventTests(TestCase):
    """A sweep confirming nothing changed must not write an audit row.

    `confirm_observed` stamps `last_observed_at` on every declaration it
    matches, every pass. Audited as a change that is one row per resource per
    sweep: at a sixty-second interval, thousands a day saying "checked, still
    fine", with every real event buried among them.
    """

    def setUp(self):
        self.principal = cli_principal()
        self.resource = ManagedResource.objects.create(key="watched", kind="adguard.rewrite", spec=dict(A_REWRITE))
        self.before = audit_rows.mark()

    def sweep(self, record=None):
        record_sweep(a_sweep(**{"adguard.rewrite": [record or A_REWRITE]}), principal=self.principal)
        self.resource.refresh_from_db()

    def updates(self):
        return audit_rows.since(self.before).filter(object_type="Managed resource", action=AuditLog.Action.UPDATED)

    def test_confirming_an_unchanged_declaration_writes_the_stamp_and_no_audit_row(self):
        """Silencing the event must not silence the fact it records."""
        # The first sweep is a real event: status, conditions and the observed
        # generation all move from empty to observed. Every sweep after it
        # changes only the stamp, and those are the thousands of rows.
        self.sweep()
        first = self.resource.last_observed_at
        self.before = audit_rows.mark()
        for _ in range(5):
            self.sweep()
        self.assertIsNotNone(first)
        self.assertGreaterEqual(self.resource.last_observed_at, first)
        self.assertFalse(self.updates().exists())

    def test_a_sweep_that_finds_something_different_is_still_an_event(self):
        self.sweep()
        self.before = audit_rows.mark()
        self.sweep()
        self.assertEqual(self.updates().count(), 0, "a quiet sweep is not an event")
        ManagedResource.objects.filter(pk=self.resource.pk).update(spec={**A_REWRITE, "answer": "10.0.0.99"})
        self.sweep({**A_REWRITE, "answer": "10.0.0.99"})
        self.assertEqual(self.updates().count(), 1)
        changed = self.updates().first().metadata["changes"]
        # The stamp rides along with the real change rather than vanishing.
        self.assertIn("status", changed)
        self.assertIn("last_observed_at", changed)


class ObservationKindTests(TestCase):
    """Readings a controller reports that are not resources HQ can declare.

    The ingest drops any kind this HQ does not know, so that a controller
    running ahead of it cannot take the whole sweep down. A reading with no
    provider spec looks exactly like that case, so it must be kept rather than
    discarded while its collector runs normally.
    """

    def test_a_reading_survives_the_sweep_without_being_a_resource(self):
        reading = {
            "record": "interface-binding",
            "interface": "tailscale0",
            "accept_requires_interface": True,
            "foreign_interface_dropped": True,
        }

        record_inventory(
            a_sweep(**{"host.firewall": [reading]}),
            principal=cli_principal(),
        )

        stored = ProviderInventory.objects.get(kind="host.firewall")
        self.assertTrue(stored.reachable)
        self.assertEqual(stored.records, [reading])

    def test_it_is_still_not_something_to_declare(self):
        # Storing the reading must not put a resource on the board. Nothing can
        # create, reconcile or delete a host firewall, so an entry offering to
        # would be an action that cannot be taken.
        record_inventory(
            a_sweep(**{"host.firewall": [{"record": "interface-binding"}]}),
            principal=cli_principal(),
        )

        self.assertNotIn("host.firewall", {item.kind for item in unmanaged()})

    def test_a_kind_this_hq_has_never_heard_of_is_still_dropped(self):
        record_inventory(
            a_sweep(**{"host.something-newer": [{"record": "x"}]}),
            principal=cli_principal(),
        )

        self.assertFalse(ProviderInventory.objects.filter(kind="host.something-newer").exists())
