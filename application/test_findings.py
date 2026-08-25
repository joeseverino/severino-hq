"""The claims HQ makes about itself, and the silence each one breaks."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderConnection

from .findings import derive_findings, finding_rules, findings, rule_for
from .security import Capability, Principal
from .topology import derive_topology


READ = Principal("reader", "test", frozenset({Capability.READ}))
MANAGE = Principal(
    "operator",
    "test",
    frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}),
)
NONE = Principal("nobody", "test", frozenset())


def observed(
    resource: ManagedResource, when, *, reason="Observed", revision=1, status=None
):
    """Write the state a sweep or a reconcile would have left behind.

    A real sweep writes the whole rebuilt spec into ``status``; a reconcile
    writes a thinner summary. Defaulting to the full spec keeps the fixture
    honest, because "observed but confirming nothing" is a genuine finding and
    should not be the accidental default of every test.
    """

    ManagedResource.objects.filter(pk=resource.pk).update(
        last_observed_at=when,
        generation=revision,
        observed_generation=revision,
        status=dict(resource.spec) if status is None else status,
        conditions=[
            {"type": "Ready", "status": True, "reason": reason, "message": ""}
        ],
    )


class FindingsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        ProviderConnection.objects.create(
            controller_id="example-controller",
            connection_ref="example-cloudflare",
            provider="cloudflare_dns",
            endpoint="https://api.example.test/client/v4",
            reaches=["example.com"],
            reachable=True,
            probed=True,
            observed_at=self.now,
        )

    def rewrite(self, key, **spec):
        return ManagedResource.objects.create(
            key=key,
            kind="adguard.rewrite",
            spec={"domain": f"{key}.example.test", "answer": "192.0.2.10", **spec},
        )

    def raised(self, principal=MANAGE):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            projection = derive_topology(principal=principal)
        return derive_findings(projection, principal=principal)

    def names(self, principal=MANAGE):
        return [(f.rule, f.subject) for f in self.raised(principal)]

    # ----- the archetype ---------------------------------------------------

    def test_the_record_a_sweep_skipped_is_reported_with_its_evidence(self):
        """The exact state that hid two hosts for days.

        Healthy condition, revisions equal so nothing queues a reconcile, and
        the only moving fact is the observation falling behind its siblings.
        """

        skipped = self.rewrite("skipped-record")
        for index in range(3):
            observed(self.rewrite(f"swept-{index}"), self.now, reason="Observed")
        observed(skipped, self.now - timedelta(hours=6), reason="Reconciled")

        found = [f for f in self.raised() if f.rule == "skipped-by-a-sweep"]

        self.assertEqual([f.subject for f in found], ["resource:skipped-record"])
        self.assertEqual(found[0].severity, "serious")
        evidence = dict(found[0].evidence)
        self.assertEqual(evidence["Behind by"], "6h")
        self.assertEqual(evidence["Condition reason"], "Reconciled")
        self.assertEqual(evidence["Records of this kind observed"], "4")

    def test_the_skipped_record_is_offered_a_reconcile(self):
        skipped = self.rewrite("skipped-record")
        observed(self.rewrite("swept"), self.now)
        observed(skipped, self.now - timedelta(hours=6))

        found = next(f for f in self.raised() if f.rule == "skipped-by-a-sweep")
        remedy = found.remedies[0]

        self.assertEqual(remedy.capability, "infrastructure.reconcile")
        self.assertEqual(remedy.target, "skipped-record")
        # Effect is copied from the capability registry, never restated here.
        self.assertTrue(remedy.effect)

    def test_a_healthy_estate_says_nothing(self):
        """A rule that cannot be quiet is a lens, not a finding."""

        for index in range(4):
            observed(self.rewrite(f"fine-{index}"), self.now)

        self.assertEqual(self.raised(), ())

    # ----- the blind spot the sibling test has -----------------------------

    def test_a_kind_nothing_has_swept_is_reported_even_with_no_sibling(self):
        """One record of a kind has nothing to be behind.

        The sibling comparison is structurally blind here, which is why the two
        rules ship together.
        """

        lonely = self.rewrite("only-of-its-kind")
        observed(lonely, self.now - timedelta(days=3))

        found = [f for f in self.raised() if f.rule == "kind-never-swept"]

        self.assertEqual([f.scope for f in found], ["adguard.rewrite"])
        self.assertEqual(found[0].subject, "")

    def test_a_sweep_wide_outage_is_said_once_about_the_kind(self):
        """Not once per record. A queue that triples is a queue nobody reads."""

        for index in range(5):
            observed(self.rewrite(f"stale-{index}"), self.now - timedelta(days=3))

        rules = [f.rule for f in self.raised()]

        self.assertEqual(rules.count("kind-never-swept"), 1)
        self.assertNotIn("skipped-by-a-sweep", rules)
        self.assertNotIn("never-observed", rules)

    # ----- the other two rules ---------------------------------------------

    def test_a_declaration_that_keeps_disagreeing_points_at_itself(self):
        resource = self.rewrite("wont-converge")
        # `resource_health` reads only conditions whose status is true, so a
        # failure is an active `Degraded`, not a falsified `Ready`.
        ManagedResource.objects.filter(pk=resource.pk).update(
            last_observed_at=self.now,
            generation=4,
            observed_generation=4,
            conditions=[
                {
                    "type": "Degraded",
                    "status": True,
                    "reason": "Failed",
                    "message": "The provider refused it.",
                }
            ],
        )

        found = [f for f in self.raised() if f.rule == "reconciled-but-still-wrong"]

        self.assertEqual([f.subject for f in found], ["resource:wont-converge"])
        evidence = dict(found[0].evidence)
        self.assertEqual(evidence["Declared revision"], "4")
        self.assertEqual(evidence["Observed revision"], "4")

    def test_never_observed_needs_something_that_could_have_looked(self):
        """Uncovered and skipped are different findings with different answers."""

        # The controller declares an ability per provider kind, so a kind with a
        # provider is always governed. A kind no provider claims is the only way
        # to be genuinely uncovered.
        uncovered = ManagedResource.objects.create(
            key="nothing-governs-me", kind="example.unclaimed", spec={}
        )
        governed = ManagedResource.objects.create(
            key="governed-but-unseen",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "example-cloudflare"},
        )
        # A sibling of the same kind that HAS been observed. Without one the gap
        # belongs to the kind, and `kind-never-swept` speaks for it instead.
        observed(
            ManagedResource.objects.create(
                key="governed-and-seen",
                kind="cloudflare.zone",
                spec={"zone": "seen.example", "connection_ref": "example-cloudflare"},
            ),
            self.now,
        )
        observed(self.rewrite("sibling"), self.now)

        subjects = [f.subject for f in self.raised() if f.rule == "never-observed"]

        self.assertIn(f"resource:{governed.key}", subjects)
        self.assertNotIn(f"resource:{uncovered.key}", subjects)

    def test_a_kind_nothing_ever_reached_is_one_claim_not_one_per_record(self):
        """The shape that produced 320 findings against the real estate.

        A kind with no observation at all has no newest to be behind, so the
        sibling comparison cannot see it -- and every record of it is "never
        observed" on its own. Said once about the kind it is one line; said per
        record it is a queue nobody reads, which is the original bug wearing a
        different hat.
        """

        for index in range(25):
            ManagedResource.objects.create(
                key=f"unreached-{index}",
                kind="cloudflare.zone",
                spec={
                    "zone": f"z{index}.example",
                    "connection_ref": "example-cloudflare",
                },
            )
        observed(self.rewrite("a-kind-that-works"), self.now)

        raised = self.raised()
        by_rule = [f.rule for f in raised]

        self.assertEqual(by_rule.count("kind-never-swept"), 1)
        self.assertEqual(by_rule.count("never-observed"), 0)
        claim = next(f for f in raised if f.rule == "kind-never-swept")
        self.assertEqual(claim.scope, "cloudflare.zone")
        self.assertEqual(dict(claim.evidence)["Records of this kind"], "25")

    def test_a_record_confirming_only_some_of_what_it_asserts_is_reported(self):
        """The security shape: observed, healthy, and asserting unchecked facts.

        Drift is judged only where both sides speak, so a field the reading
        omits is never compared. On the real estate the two proxy hosts holding
        the only `block_exploits` were confirming two fields out of seventeen,
        and nothing said so.
        """

        partly = self.rewrite("half-checked")
        observed(partly, self.now, status={"domain": partly.spec["domain"]})
        observed(self.rewrite("fully-checked"), self.now)

        found = [f for f in self.raised() if f.rule == "weakly-verified"]

        self.assertEqual([f.subject for f in found], ["resource:half-checked"])
        self.assertEqual(dict(found[0].evidence)["Unconfirmed"], "answer")
        # `field(s)` is what a claim looks like when it does not know how
        # many there are. This one does.
        self.assertTrue(found[0].title.endswith("1 unconfirmed field"))
        self.assertNotIn("(s)", found[0].title)

    def test_a_field_carrying_no_value_is_not_an_unconfirmed_assertion(self):
        """A spec is a full model dump, so an optional field nobody set is
        still a key. Reading that as a claim put twenty-eight unclearable
        findings in front of the real ones on the live estate: every DNS record
        that was not an MX asserted a ``priority`` the provider correctly
        declines to read back for a type that has none."""

        record = ManagedResource.objects.create(
            key="nothing-asserted",
            kind="cloudflare.dns_record",
            spec={
                "zone": "example.test",
                "name": "example.test",
                "record_type": "TXT",
                "content": '"v=spf1 -all"',
                "priority": None,
                "proxied": False,
                "ttl": 1,
            },
        )
        # A sweep that read everything the record actually has.
        observed(
            record,
            self.now,
            status={
                "zone": "example.test",
                "name": "example.test",
                "record_type": "TXT",
                "content": '"v=spf1 -all"',
                "proxied": False,
                "ttl": 1,
            },
        )

        subjects = [f.subject for f in self.raised() if f.rule == "weakly-verified"]

        self.assertNotIn("resource:nothing-asserted", subjects)

    def test_a_field_carrying_a_value_is_still_an_unconfirmed_assertion(self):
        """The other half of the same rule, and the half worth reading.

        ``False`` and ``0`` are not absence. A record that says a port is
        served, or that key expiry is off, has said something checkable that
        nothing has checked -- and silencing those alongside the empty ones is
        how the fix above would have become the bug it was fixing.
        """

        record = ManagedResource.objects.create(
            key="something-asserted",
            kind="cloudflare.dns_record",
            spec={
                "zone": "example.test",
                "name": "mail.example.test",
                "record_type": "MX",
                "content": "mx.example.test",
                "priority": 0,
                "proxied": False,
                "ttl": 1,
            },
        )
        observed(
            record,
            self.now,
            status={
                "zone": "example.test",
                "name": "mail.example.test",
                "record_type": "MX",
                "content": "mx.example.test",
                "proxied": False,
                "ttl": 1,
            },
        )

        found = [
            f
            for f in self.raised()
            if f.rule == "weakly-verified" and f.subject == "resource:something-asserted"
        ]

        self.assertEqual(dict(found[0].evidence)["Unconfirmed"], "priority")

    def test_key_expiry_is_confirmed_by_the_sweep_that_can_see_it(self):
        """The daemon reading holds presence and key expiry -- "the two that go
        wrong quietly" -- and the record mapping kept only the name, so every
        device asserted a setting no sweep confirmed."""

        from control_plane.providers import PROVIDERS

        rebuilt = PROVIDERS["tailscale.device"].from_record(
            {"name": "example-device", "key_expires": "2026-12-01T00:00:00Z"}
        )
        self.assertIs(rebuilt["key_expiry_disabled"], False)

        # No expiry is the setting, not an unknown date.
        disabled = PROVIDERS["tailscale.device"].from_record({"name": "forever"})
        self.assertIs(disabled["key_expiry_disabled"], True)

    def test_ports_only_the_operator_can_know_are_a_declared_gap(self):
        """``serves_ports`` exists for containers sharing the machine's network,
        which are exactly the ones Docker publishes no ports for. A sweep can
        never echo it back, so it is declared rather than reported forever."""

        from control_plane.providers import PROVIDERS

        self.assertIn(
            "serves_ports", PROVIDERS["portainer.container"].unobservable_fields
        )

        container = ManagedResource.objects.create(
            key="example-container",
            kind="portainer.container",
            spec={
                "connection_ref": "example-portainer",
                "host": "example-host",
                "name": "example-web",
                "serves_ports": [8080],
            },
        )
        observed(
            container,
            self.now,
            status={
                "connection_ref": "example-portainer",
                "host": "example-host",
                "name": "example-web",
            },
        )

        subjects = [f.subject for f in self.raised() if f.rule == "weakly-verified"]

        self.assertNotIn("resource:example-container", subjects)

    def test_a_field_the_provider_declared_it_cannot_report_is_not_a_finding(self):
        """A known gap is not a silent one, and only silence is the bug."""

        from control_plane.providers import PROVIDERS

        self.assertIn(
            "certificate_resource", PROVIDERS["npm.proxy_host"].unobservable_fields
        )
        proxy = ManagedResource.objects.create(
            key="declared-gap",
            kind="npm.proxy_host",
            spec={
                **PROVIDERS["npm.proxy_host"].from_record(
                    PROVIDERS["npm.proxy_host"].sample_record
                ),
                "certificate_resource": "example-wildcard",
            },
        )
        # A full sweep result, which by construction never carries the field.
        observed(
            proxy,
            self.now,
            status=PROVIDERS["npm.proxy_host"].from_record(
                PROVIDERS["npm.proxy_host"].sample_record
            ),
        )

        subjects = [f.subject for f in self.raised() if f.rule == "weakly-verified"]

        self.assertNotIn("resource:declared-gap", subjects)

    def test_a_disabled_declaration_is_not_a_finding(self):
        """Nobody asked for it to be true."""

        paused = self.rewrite("paused")
        ManagedResource.objects.filter(pk=paused.pk).update(enabled=False)
        observed(paused, self.now - timedelta(hours=8))
        observed(self.rewrite("active"), self.now)

        self.assertNotIn(
            "resource:paused", [subject for _, subject in self.names()]
        )

    # ----- properties that must hold ---------------------------------------

    def test_deriving_findings_costs_no_query_and_changes_nothing(self):
        from django.db import connection as database_connection
        from django.test.utils import CaptureQueriesContext

        observed(self.rewrite("one"), self.now)
        observed(self.rewrite("two"), self.now - timedelta(hours=9))
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            projection = derive_topology(principal=MANAGE)
        before = list(
            ManagedResource.objects.values_list("key", "last_observed_at", "conditions")
        )

        with CaptureQueriesContext(database_connection) as captured:
            derive_findings(projection, principal=MANAGE)

        self.assertEqual(len(captured), 0)
        self.assertEqual(
            before,
            list(
                ManagedResource.objects.values_list(
                    "key", "last_observed_at", "conditions"
                )
            ),
        )

    def test_a_principal_who_cannot_run_it_is_offered_nothing(self):
        """Absent, not disabled. An offer that cannot work is worse than none."""

        skipped = self.rewrite("skipped-record")
        observed(self.rewrite("swept"), self.now)
        observed(skipped, self.now - timedelta(hours=6))

        as_operator = self.raised(MANAGE)
        as_reader = self.raised(READ)

        self.assertTrue(any(f.remedies for f in as_operator))
        self.assertTrue(as_reader, "the reader still sees the finding")
        self.assertEqual([f.remedies for f in as_reader], [() for _ in as_reader])

    def test_findings_cannot_widen_what_the_principal_could_see(self):
        observed(self.rewrite("one"), self.now - timedelta(hours=9))
        observed(self.rewrite("two"), self.now)

        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            visible = {n.id for n in derive_topology(principal=READ).nodes}
        subjects = {f.subject for f in self.raised(READ) if f.subject}

        self.assertLessEqual(subjects, visible)

    def test_no_rule_offers_a_destructive_capability(self):
        """A finding may link to a review page; it may never hand over a delete."""

        from .capabilities import capability_specs

        destructive = {
            spec.name for spec in capability_specs() if spec.effect == "destructive"
        }
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        offered = {
            remedy.capability for f in self.raised() for remedy in f.remedies
        }

        self.assertEqual(offered & destructive, set())

    def test_every_remedy_names_a_capability_the_registry_holds(self):
        from .capabilities import capability_specs

        known = {spec.name for spec in capability_specs()}
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        for finding in self.raised():
            for remedy in finding.remedies:
                with self.subTest(rule=finding.rule):
                    self.assertIn(remedy.capability, known)

    def test_no_rule_names_an_installed_extension(self):
        """The host does not name its extensions, stated as a test."""

        from control_plane.providers import PROVIDERS

        vocabulary = {kind.split(".")[0] for kind in PROVIDERS}
        for rule in finding_rules():
            with self.subTest(rule=rule.name):
                text = f"{rule.name} {rule.title}".lower()
                self.assertFalse(
                    [word for word in vocabulary if word in text.split()],
                    f"{rule.name} names a provider family",
                )

    def test_serialization_names_the_rule_and_every_available_one(self):
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            whole = findings(principal=MANAGE)
            narrowed = findings(principal=MANAGE, rule="skipped-by-a-sweep")
            unknown = findings(principal=MANAGE, rule="not-a-rule")

        self.assertEqual(narrowed["rule"], "skipped-by-a-sweep")
        self.assertIsNone(unknown["rule"])
        self.assertEqual(unknown["summary"], whole["summary"])
        self.assertEqual(
            [item["name"] for item in whole["rules"]],
            [rule.name for rule in finding_rules()],
        )
        self.assertNotIn("secret", json.dumps(whole).lower())

    def test_an_unknown_rule_name_does_not_resolve(self):
        self.assertIsNone(rule_for("not-a-rule"))
        self.assertIsNotNone(rule_for("skipped-by-a-sweep"))


class AutoRepairTests(TestCase):
    """What HQ will queue on its own, and everything that stops it.

    HQ queues; the controller pulls and claims. Nothing here executes, and the
    graph -- not a guess -- decides whether acting is sane.
    """

    def setUp(self):
        self.now = timezone.now()
        ProviderConnection.objects.create(
            controller_id="example-controller",
            connection_ref="example-adguard",
            provider="adguard",
            endpoint="http://192.0.2.5",
            reaches=["adguard"],
            reachable=True,
            probed=True,
            observed_at=self.now,
        )

    def rewrite(self, key):
        return ManagedResource.objects.create(
            key=key,
            kind="adguard.rewrite",
            spec={"domain": f"{key}.example.test", "answer": "192.0.2.10"},
        )

    def repairs(self):
        from .findings import auto_remediable

        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            return auto_remediable(principal=MANAGE)

    def test_a_skipped_record_is_offered_for_repair(self):
        skipped = self.rewrite("skipped")
        observed(skipped, self.now - timedelta(hours=6))
        for index in range(3):
            observed(self.rewrite(f"swept-{index}"), self.now)

        self.assertEqual(
            [(r.resource_key, r.rule) for r in self.repairs()],
            [("skipped", "skipped-by-a-sweep")],
        )

    def test_a_kind_wide_outage_queues_nothing(self):
        """The amplifier guard. Everything stale means the sweep is the fault.

        Repairing each record would fan the whole class at a provider that is
        not answering -- turning one fault into an outage-shaped retry storm.
        """

        for index in range(6):
            observed(self.rewrite(f"stale-{index}"), self.now - timedelta(days=3))

        self.assertEqual(self.repairs(), ())

    def test_nothing_is_queued_for_a_kind_no_live_connection_governs(self):
        """Traversed, not assumed: connection -> enables -> ability -> governs."""

        ProviderConnection.objects.update(reachable=False, probed=True)
        skipped = self.rewrite("skipped")
        observed(skipped, self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        self.assertEqual(self.repairs(), ())

    def test_the_number_queued_in_one_pass_is_capped(self):
        from .findings import auto_remediable

        for index in range(25):
            observed(self.rewrite(f"skipped-{index}"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            self.assertEqual(len(auto_remediable(principal=MANAGE, limit=4)), 4)

    def test_only_actions_the_controller_contract_runs_unattended_qualify(self):
        """HQ forms no second opinion about unattended safety."""

        from control_plane.models import OperationRequest
        from control_plane.providers import enabled_controller_actions

        automatic = {
            kind
            for kind, action in enabled_controller_actions(automatic_only=True)
            if action == OperationRequest.Action.RECONCILE
        }
        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        for repair in self.repairs():
            resource = ManagedResource.objects.get(key=repair.resource_key)
            with self.subTest(key=repair.resource_key):
                self.assertIn(resource.kind, automatic)

    def test_scheduling_is_off_unless_the_deployment_turns_it_on(self):
        from django.test import override_settings

        from .controller import schedule_automatic_operations
        from control_plane.models import OperationRequest

        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with override_settings(SEVERINO_FINDINGS_AUTO_REMEDY=False):
            with mock.patch(
                "application.plugins.plugin_connection_specs", return_value=()
            ):
                answer = schedule_automatic_operations("example-controller")

        self.assertEqual(answer["repaired"], [])
        self.assertFalse(
            OperationRequest.objects.filter(
                idempotency_key__startswith="finding:"
            ).exists()
        )

    def test_turned_on_it_queues_once_and_only_once(self):
        from django.test import override_settings

        from .controller import schedule_automatic_operations
        from control_plane.models import OperationRequest

        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with override_settings(SEVERINO_FINDINGS_AUTO_REMEDY=True):
            with mock.patch(
                "application.plugins.plugin_connection_specs", return_value=()
            ):
                first = schedule_automatic_operations("example-controller")
                second = schedule_automatic_operations("example-controller")

        self.assertEqual(first["repaired"], ["skipped"])
        # Keyed on the evidence rather than the attempt, so a second pass over
        # the same unchanged finding adds nothing.
        self.assertEqual(second["repaired"], [])
        queued = OperationRequest.objects.filter(
            idempotency_key__startswith="finding:"
        )
        self.assertEqual(queued.count(), 1)
        self.assertEqual(queued.first().requested_interface, "controller")
        self.assertEqual(queued.first().action, OperationRequest.Action.RECONCILE)

    def test_hq_queues_and_never_executes(self):
        """Trust direction. HQ queues, the controller pulls and claims."""

        from django.test import override_settings

        from .controller import schedule_automatic_operations
        from control_plane.models import OperationRequest

        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with override_settings(SEVERINO_FINDINGS_AUTO_REMEDY=True):
            with mock.patch(
                "application.plugins.plugin_connection_specs", return_value=()
            ):
                schedule_automatic_operations("example-controller")

        operation = OperationRequest.objects.get(
            idempotency_key__startswith="finding:"
        )
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)
