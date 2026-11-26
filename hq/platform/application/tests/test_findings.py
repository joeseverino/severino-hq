"""The claims HQ makes about itself, and the silence each one breaks."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from pydantic import ValidationError

from hq.domains.control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from hq.platform.core.testing import MANAGER, READER, connection, declare

from ..findings import FINDINGS_SCHEMA_VERSION, derive_findings, finding_rules, findings, rule_for
from ..topology import derive_topology
from ..topology_model import Topology, TopologyEdge, TopologyNode
from .host_only import host_only
from .inventory_records import kind_read
from .sentinel import SentinelMixin, plant_credentials


def observed(resource: ManagedResource, when, *, reason="Observed", revision=1, status=None):
    """Write the state a sweep or a reconcile would have left behind.

    A real sweep writes the whole rebuilt spec into ``status``; a reconcile
    writes a thinner summary. Defaulting to the full spec keeps the fixture
    honest, because "observed but confirming nothing" is a genuine finding and
    should not be the accidental default of every test. The kind counts as read
    at the newest moment any of its records was.
    """

    kind_read(resource.kind, when)
    ManagedResource.objects.filter(pk=resource.pk).update(
        last_observed_at=when,
        generation=revision,
        observed_generation=revision,
        status=dict(resource.spec) if status is None else status,
        conditions=[{"type": "Ready", "status": True, "reason": reason, "message": ""}],
    )


class FindingsTests(SentinelMixin, TestCase):
    def setUp(self):
        self.now = timezone.now()
        connection(
            "cloudflare_dns",
            "example-cloudflare",
            endpoint="https://api.example.test/client/v4",
            reaches=["example.com"],
            observed_at=self.now,
        )

    def rewrite(self, key, **spec):
        return declare(key, "adguard.rewrite", {"domain": f"{key}.example.test", "answer": "192.0.2.10", **spec})

    def raised(self, principal=MANAGER):
        with host_only():
            projection = derive_topology(principal=principal)
        return derive_findings(projection, principal=principal)

    def names(self, principal=MANAGER):
        return [(f.rule, f.subject) for f in self.raised(principal)]

    # ----- the archetype ---------------------------------------------------

    def test_the_only_record_of_its_type_left_behind_names_no_others(self):
        observed(self.rewrite("sole"), self.now - timedelta(hours=6))
        kind_read("adguard.rewrite", self.now)

        (found,) = [f for f in self.raised() if f.rule == "skipped-by-a-sweep"]

        said = " ".join((found.explanation, *(label for label, _value in found.evidence)))
        self.assertNotIn("other", said.lower())

    # ----- the blind spot the sibling test has -----------------------------

    def test_a_kind_only_ever_failing_to_read_has_never_been_read(self):
        """The row a failed first read writes holds no moment."""

        self.rewrite("never-reached")
        ProviderInventory.objects.create(
            kind="adguard.rewrite", reachable=False, records=[], error="unreachable", observed_at=None
        )

        (found,) = [f for f in self.raised() if f.rule == "kind-never-swept"]

        self.assertEqual(dict(found.evidence)["Last read"], "never")

    def test_a_sweep_wide_outage_is_said_once_about_the_kind(self):
        """Not once per record. A queue that triples is a queue nobody reads."""

        for index in range(5):
            observed(self.rewrite(f"stale-{index}"), self.now - timedelta(days=3))

        rules = [f.rule for f in self.raised()]

        self.assertEqual(rules.count("kind-never-swept"), 1)
        self.assertNotIn("skipped-by-a-sweep", rules)
        self.assertNotIn("never-observed", rules)

    def test_the_controller_is_looked_at_with_commands_that_paste(self):
        from ..controller_findings import _controller_steps

        self.assertEqual(
            [step.command for step in _controller_steps("lab-1")],
            [
                'ssh lab-1 "sudo systemctl status severino-hq-controller.service --no-pager"',
                'ssh lab-1 "sudo journalctl -u severino-hq-controller.service -n 50 --no-pager"',
            ],
        )

    def test_a_shared_ability_does_not_guess_between_two_controllers(self):
        old = (self.now - timedelta(days=3)).isoformat()
        topology = Topology(
            nodes=(
                TopologyNode("controller:one", "controller", "One", "Controller"),
                TopologyNode("controller:two", "controller", "Two", "Controller"),
                TopologyNode("connection:one", "connection", "One", "Connection"),
                TopologyNode("connection:two", "connection", "Two", "Connection"),
                TopologyNode("ability:shared", "ability", "Read", "Ability"),
                TopologyNode(
                    "resource:a",
                    "resource",
                    "A",
                    "Resource",
                    kind_key="example.a",
                    observed_at=old,
                ),
                TopologyNode(
                    "resource:b",
                    "resource",
                    "B",
                    "Resource",
                    kind_key="example.b",
                    observed_at=old,
                ),
            ),
            edges=(
                TopologyEdge("carries-one", "controller:one", "connection:one", "carries", "Carries"),
                TopologyEdge("carries-two", "controller:two", "connection:two", "carries", "Carries"),
                TopologyEdge("enables-one", "connection:one", "ability:shared", "enables", "Enables"),
                TopologyEdge("enables-two", "connection:two", "ability:shared", "enables", "Enables"),
                TopologyEdge("governs-a", "ability:shared", "resource:a", "governs", "Governs"),
                TopologyEdge("governs-b", "ability:shared", "resource:b", "governs", "Governs"),
            ),
        )

        raised = derive_findings(topology, principal=READER)

        self.assertEqual([item.rule for item in raised], ["kind-never-swept"] * 2)

    # ----- the other two rules ---------------------------------------------

    def test_key_expiry_is_confirmed_by_the_sweep_that_can_see_it(self):
        """The daemon reading holds presence and key expiry: "the two that go
        wrong quietly". The record mapping carries key expiry too, or every
        device asserts a setting no sweep confirms."""

        from hq.domains.control_plane.providers import PROVIDERS

        rebuilt = PROVIDERS["tailscale.device"].record_spec(
            {"name": "example-device", "key_expires": "2026-12-01T00:00:00Z"}
        )
        self.assertIs(rebuilt["key_expiry_disabled"], False)

        # No expiry is the setting, not an unknown date.
        disabled = PROVIDERS["tailscale.device"].record_spec({"name": "forever"})
        self.assertIs(disabled["key_expiry_disabled"], True)

    # ----- properties that must hold ---------------------------------------

    def test_deriving_findings_changes_nothing(self):
        observed(self.rewrite("one"), self.now)
        observed(self.rewrite("two"), self.now - timedelta(hours=9))
        with host_only():
            projection = derive_topology(principal=MANAGER)
        before = list(ManagedResource.objects.values_list("key", "last_observed_at", "conditions"))

        derive_findings(projection, principal=MANAGER)

        self.assertEqual(
            before,
            list(ManagedResource.objects.values_list("key", "last_observed_at", "conditions")),
        )

    def test_a_principal_who_cannot_run_it_is_offered_nothing(self):
        """Absent, not disabled. An offer that cannot work is worse than none."""

        skipped = self.rewrite("skipped-record")
        observed(self.rewrite("swept"), self.now)
        observed(skipped, self.now - timedelta(hours=6))

        as_operator = self.raised(MANAGER)
        as_reader = self.raised(READER)

        self.assertTrue(any(list(f.presses) for f in as_operator))
        self.assertTrue(as_reader, "the reader still sees the finding")
        self.assertEqual([list(f.presses) for f in as_reader], [[] for _ in as_reader])

    def test_findings_cannot_widen_what_the_principal_could_see(self):
        observed(self.rewrite("one"), self.now - timedelta(hours=9))
        observed(self.rewrite("two"), self.now)

        with host_only():
            visible = {n.id for n in derive_topology(principal=READER).nodes}
        subjects = {f.subject for f in self.raised(READER) if f.subject}

        self.assertLessEqual(subjects, visible)

    def test_no_rule_offers_a_destructive_capability(self):
        """A finding may link to a review page; it may never hand over a delete."""

        from ..capabilities import capability_registry

        destructive = {spec.name for spec in capability_registry() if spec.effect == "destructive"}
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        offered = {remedy.capability for f in self.raised() for remedy in f.remedies}

        self.assertEqual(offered & destructive, set())

    def test_every_remedy_names_a_capability_the_registry_holds(self):
        from ..capabilities import capability_registry

        known = {spec.name for spec in capability_registry()}
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        for finding in self.raised():
            for remedy in finding.remedies:
                with self.subTest(rule=finding.rule):
                    self.assertIn(remedy.capability, known)

    def test_no_rule_names_an_installed_extension(self):
        """The host does not name its extensions, stated as a test."""

        from hq.domains.control_plane.providers import PROVIDERS

        vocabulary = {kind.split(".")[0] for kind in PROVIDERS}
        for rule in finding_rules():
            with self.subTest(rule=rule.name):
                text = f"{rule.name} {rule.title}".lower()
                named = sorted(word for word in vocabulary if word in text.split())
                self.assertFalse(
                    named,
                    f"{rule.name} names the provider family {', '.join(named)} in its name or title; "
                    "a host rule says what is wrong in words no provider owns",
                )

    def test_serialization_names_the_rule_it_was_narrowed_by(self):
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        with host_only():
            whole = findings(principal=MANAGER)
            narrowed = findings(principal=MANAGER, rule="skipped-by-a-sweep")

        self.assertEqual(narrowed["rule"], "skipped-by-a-sweep")
        self.assertIsNone(whole["rule"])
        self.assertEqual(whole["schema_version"], FINDINGS_SCHEMA_VERSION)
        self.assertTrue(any(item["offers"] for item in whole["findings"]))
        self.assertTrue(any(item["investigations"] for item in whole["findings"]))
        self.assertTrue(all(item["id"] for item in whole["findings"]))
        presses = [step["link"] for item in whole["findings"] for step in item["steps"] if step["kind"] == "press"]
        self.assertTrue(presses)
        self.assertTrue(all(press["capability"] for press in presses))

    def test_no_finding_carries_a_credential_a_controller_reported(self):
        plant_credentials()
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)

        with host_only():
            whole = findings(principal=MANAGER)

        self.assertTrue(whole["findings"])
        self.assertNoSentinel(whole)

    def test_a_floor_and_a_limit_narrow_what_is_returned_and_never_the_totals(self):
        observed(self.rewrite("skipped"), self.now - timedelta(hours=9))
        observed(self.rewrite("swept"), self.now)
        self.rewrite("never")

        with host_only():
            whole = findings(principal=MANAGER)
            floored = findings(principal=MANAGER, severity="serious")
            first = findings(principal=MANAGER, limit=1)
        severities = [item["severity"] for item in whole["findings"]]
        # The estate has something below the floor and more than one finding.
        self.assertIn("serious", severities)
        self.assertTrue(set(severities) - {"serious"})

        for narrowed in (floored, first):
            self.assertEqual(narrowed["summary"]["severities"], whole["summary"]["severities"])
            self.assertEqual(narrowed["summary"]["findings"], whole["summary"]["findings"])
            self.assertEqual(narrowed["summary"]["returned"], len(narrowed["findings"]))
        self.assertEqual(
            [item["id"] for item in floored["findings"]],
            [item["id"] for item in whole["findings"] if item["severity"] == "serious"],
        )
        self.assertFalse(floored["summary"]["truncated"])
        self.assertEqual([item["id"] for item in first["findings"]], [whole["findings"][0]["id"]])
        self.assertTrue(first["summary"]["truncated"])
        self.assertFalse(whole["summary"]["truncated"])

    def test_a_severity_off_the_ladder_or_a_limit_below_one_is_refused(self):
        for narrowing in ({"severity": "critical"}, {"limit": 0}):
            with self.subTest(**narrowing), self.assertRaises(ValidationError) as refused:
                findings(principal=MANAGER, **narrowing)
            self.assertEqual([error["loc"] for error in refused.exception.errors()], [tuple(narrowing)])

    def test_an_unknown_rule_name_does_not_resolve(self):
        self.assertIsNone(rule_for("not-a-rule"))
        self.assertIsNotNone(rule_for("skipped-by-a-sweep"))


class AutoRepairTests(TestCase):
    """What HQ will queue on its own, and everything that stops it.

    HQ queues; the controller pulls and claims. Nothing here executes, and the
    graph (not a guess) decides whether acting is sane.
    """

    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        self.now = timezone.now()
        connection(
            "adguard",
            "example-adguard",
            endpoint="http://192.0.2.5",
            reaches=["adguard"],
            manages=True,
            observed_at=self.now,
        )

    def rewrite(self, key):
        return declare(key, "adguard.rewrite", {"domain": f"{key}.example.test", "answer": "192.0.2.10"})

    def repairs(self):
        from ..findings import auto_remediable

        with host_only():
            return auto_remediable(principal=MANAGER)

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
        not answering: turning one fault into an outage-shaped retry storm.
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
        from ..findings import auto_remediable

        for index in range(25):
            observed(self.rewrite(f"skipped-{index}"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with host_only():
            self.assertEqual(len(auto_remediable(principal=MANAGER, limit=4)), 4)

    def test_only_actions_the_controller_contract_runs_unattended_qualify(self):
        """HQ forms no second opinion about unattended safety."""

        from hq.domains.control_plane.models import OperationRequest
        from hq.domains.control_plane.providers import enabled_controller_actions

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

        from hq.domains.control_plane.models import OperationRequest

        from ..controller import schedule_automatic_operations

        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with (
            override_settings(SEVERINO_FINDINGS_AUTO_REMEDY=False),
            host_only(),
        ):
            answer = schedule_automatic_operations("example-controller")

        self.assertEqual(answer["repaired"], [])
        self.assertFalse(OperationRequest.objects.filter(idempotency_key__startswith="finding:").exists())

    def test_turned_on_it_queues_once_and_only_once(self):
        from django.test import override_settings

        from hq.domains.control_plane.models import OperationRequest

        from ..controller import schedule_automatic_operations

        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with (
            override_settings(SEVERINO_FINDINGS_AUTO_REMEDY=True),
            host_only(),
        ):
            first = schedule_automatic_operations("example-controller")
            second = schedule_automatic_operations("example-controller")

        self.assertEqual(first["repaired"], ["skipped"])
        # Keyed on the evidence rather than the attempt, so a second pass over
        # the same unchanged finding adds nothing.
        self.assertEqual(second["repaired"], [])
        queued = OperationRequest.objects.filter(idempotency_key__startswith="finding:")
        self.assertEqual(queued.count(), 1)
        self.assertEqual(queued.first().requested_interface, "controller")
        self.assertEqual(queued.first().action, OperationRequest.Action.RECONCILE)

    def test_hq_queues_and_never_executes(self):
        """Trust direction. HQ queues, the controller pulls and claims."""

        from django.test import override_settings

        from hq.domains.control_plane.models import OperationRequest

        from ..controller import schedule_automatic_operations

        observed(self.rewrite("skipped"), self.now - timedelta(hours=6))
        observed(self.rewrite("swept"), self.now)

        with (
            override_settings(SEVERINO_FINDINGS_AUTO_REMEDY=True),
            host_only(),
        ):
            schedule_automatic_operations("example-controller")

        operation = OperationRequest.objects.get(idempotency_key__startswith="finding:")
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)
