"""A procedure is run through its own command, at HQ's one checkpoint, and
queued for the automation worker; nothing runs. Every manifest here is a
synthetic ``example.*`` one, supplied as an extension supplies it."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import CapabilityRule, ManagedResource, OperationRequest
from hq.platform.core.models import AgentIdentity, IdempotencyRecord

from ..capabilities import capability_registry, run_capability
from ..capability_policy import Rule, matrix, set_rule
from ..controller import claim_next_operation
from ..security import Capability, Principal, cli_principal
from .markup import links, text
from .procedures import APP, DB, EDGE, RESTART, RESTART_KEY, RESTART_RUN, REVISION, Supply, manifest, run_payload

# A person's own click in the browser, which is their consent.
OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
KIND = "automation.operation"
LOGS = "example.service_logs"


class RunTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def ask(self, payload, *, principal=OPERATOR, key="example-run-1", name=RESTART_RUN):
        return run_capability(name, payload, principal=principal, idempotency_key=key).answer

    def refused(self, payload, code, **asked):
        answer = self.ask(payload, **asked)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["code"], code, answer)
        return answer["error"]


class QueuedRunTests(RunTestCase):
    def test_a_run_queues_what_the_worker_needs(self):
        answer = self.ask(run_payload(APP, correlation="example-investigation-7"))

        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["queued"])
        operation = OperationRequest.objects.get()
        self.assertEqual(operation.action, OperationRequest.Action.RUN)
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)
        self.assertEqual((operation.resource.key, operation.resource.kind), (RESTART_KEY, KIND))
        self.assertEqual(
            operation.input,
            {
                "generation": operation.resource.generation,
                "operation": RESTART,
                "revision": REVISION,
                "parameters": {"service": "web"},
                "hosts": [APP],
                "correlation": "example-investigation-7",
            },
        )
        self.assertEqual(answer["operation"]["correlation"], "example-investigation-7")
        self.assertEqual(operation.resource.spec["revision"], REVISION)

    def test_the_claim_names_the_playbook_the_manifest_declares(self):
        self.supply(manifest(playbook="playbooks/linux/service_restart.yml"))
        self.ask(run_payload())

        claimed = claim_next_operation("example-worker", capabilities=((KIND, "run"),))

        self.assertEqual(claimed["resource"]["spec"]["playbook"], "playbooks/linux/service_restart.yml")
        self.assertEqual(claimed["resource"]["spec"]["revision"], REVISION)

    def test_the_procedure_s_page_says_its_playbook(self):
        from hq.domains.control_plane.provider_adapters.automation import OPERATION

        self.ask(run_payload())

        readout = {label: value for label, _, value in OPERATION.readout(ManagedResource.objects.get().spec, {})}
        self.assertEqual(readout["Playbook file"], "playbooks/service_restart.yml")

    def test_hosts_are_resolved_to_the_machine_each_is(self):
        from ..machines import Machine

        self.supply.know(Machine(APP, aliases=("web.example.com",)), Machine(DB))

        self.assertTrue(self.ask(run_payload("WEB.example.com.", APP, DB))["ok"])

        self.assertEqual(OperationRequest.objects.get().input["hosts"], [APP, DB])

    def test_the_worker_claims_it_by_its_kind_and_action(self):
        self.ask(run_payload())

        claimed = claim_next_operation("example-worker", capabilities=((KIND, "run"),))["operation"]

        self.assertEqual(claimed["id"], str(OperationRequest.objects.get().id))

    def test_a_read_procedure_runs_under_a_read_grant(self):
        self.supply(manifest(id=LOGS, effect="read", retry="safe"))

        answer = run_capability(f"automation.{LOGS}.run", run_payload(), principal=READER).answer

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(OperationRequest.objects.get().input["operation"], LOGS)

    def test_a_new_revision_pins_the_record_anew(self):
        from .procedures import NEXT_REVISION

        self.ask(run_payload())
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        self.supply(manifest(revision=NEXT_REVISION))

        self.assertTrue(self.ask(run_payload(), key="example-run-2")["ok"])

        resource = ManagedResource.objects.get(key=RESTART_KEY)
        self.assertEqual((resource.spec["revision"], resource.generation), (NEXT_REVISION, 2))


class AgentAskedRunTests(RunTestCase):
    """A procedure that changes a machine waits for a person whenever an agent
    asks for it, and no rule an operator sets for that agent lets it past."""

    CHANGES = (RESTART_RUN, "automation.example.wipe.run")

    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id="example.wipe", effect="destructive"), manifest(id=LOGS, effect="read"))
        AgentIdentity.objects.create(
            client_id=AGENT.actor, interfaces=["mcp"], granted=["read", "manage_infrastructure"]
        )
        self.user = get_user_model().objects.create_user("example-operator", password="x" * 20)

    def assert_held(self, name):
        answer = self.ask(run_payload(), principal=AGENT, name=name)

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_change_an_agent_asks_waits_for_a_person(self):
        for name in self.CHANGES:
            with self.subTest(name):
                self.assert_held(name)

    def test_no_agent_rule_can_allow_a_change(self):
        registry = capability_registry()
        for name in self.CHANGES:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    set_rule(subject=AGENT.actor, spec=registry[name], rule=Rule.ALLOW, user=self.user)
                # A rule stored past that refusal still lifts nothing.
                CapabilityRule.objects.update_or_create(
                    subject=AGENT.actor, capability=name, defaults={"rule": Rule.ALLOW}
                )
                self.assert_held(name)

    def test_the_agents_page_offers_no_allow_for_a_change(self):
        _columns, groups = matrix()
        offered = {
            row.name: {value for cell in row.cells for value, _ in cell.options}
            for group in groups
            for row in group.rows
        }

        for name in self.CHANGES:
            self.assertNotIn(Rule.ALLOW, offered[name], name)
        self.assertIn(Rule.ALLOW, offered[f"automation.{LOGS}.run"])


class DoorbellTests(RunTestCase):
    """A queued run rings the automation worker's doorbell once it is stored, and nothing else does."""

    def setUp(self):
        super().setUp()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(override_settings(SEVERINO_CONTROLLER_DOORBELL=str(directory / "controller-doorbell")))
        self.marker = directory / "automation-doorbell"

    def test_a_queued_run_rings_once_it_commits(self):
        with self.captureOnCommitCallbacks() as committed:
            self.assertTrue(self.ask(run_payload())["queued"])
            self.assertFalse(self.marker.exists(), "The doorbell rang before the run was stored.")

        for callback in committed:
            callback()
        self.assertTrue(self.marker.exists())

    def test_a_refused_run_does_not_ring(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(run_payload())
        self.marker.unlink()

        with self.captureOnCommitCallbacks(execute=True):
            self.refused(run_payload(DB), "precondition", key="example-run-2")

        self.assertFalse(self.marker.exists())

    def test_a_retried_run_does_not_ring_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(run_payload())
        self.marker.unlink()
        # Past the replay record, so the run's own key is what answers.
        IdempotencyRecord.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            self.ask(run_payload())

        self.assertEqual(OperationRequest.objects.count(), 1)
        self.assertFalse(self.marker.exists())


class RefusedRunTests(RunTestCase):
    def test_a_read_grant_cannot_run_a_change(self):
        self.refused(run_payload(), "forbidden", principal=READER)

        self.assertFalse(OperationRequest.objects.exists())

    def test_an_unknown_procedure_is_no_command(self):
        self.refused(run_payload(), "unknown_capability", name="automation.example.unknown.run")

    def test_a_machine_the_procedure_is_not_written_for_is_refused(self):
        self.supply(manifest(hosts=(DB,)))

        error = self.refused(run_payload(APP), "precondition")

        self.assertEqual(error["details"]["refused"], [APP])
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_kind_the_procedure_is_not_written_for_is_refused(self):
        self.supply(manifest(target_kinds=("example.vm",)))

        self.refused(run_payload(APP), "precondition")

    def test_a_machine_the_worker_has_no_login_to_as_the_run_s_profile_is_refused_when_asked(self):
        self.supply(
            manifest(),
            manifest(id="example.package_upgrade", privilege="example.package_admin"),
            manifest(id=LOGS, effect="read", retry="safe"),
        )
        # The read login to the app, and the change login to the database.
        self.supply.reach((APP, "read"), (DB, "example.package_admin"))

        unprivileged = self.refused(run_payload(APP, DB), "precondition")
        privileged = self.refused(
            run_payload(APP), "precondition", name="automation.example.package_upgrade.run", key="example-run-2"
        )
        self.assertEqual(unprivileged["details"]["missing"], [{"machine": DB, "profile": "read"}])
        self.assertEqual(privileged["details"]["missing"], [{"machine": APP, "profile": "example.package_admin"}])
        self.assertFalse(OperationRequest.objects.exists())

        # A read, and a change that names no privilege, go through the read login.
        self.assertTrue(self.ask(run_payload(APP), name=f"automation.{LOGS}.run", key="example-run-3")["queued"])
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        self.assertTrue(self.ask(run_payload(APP), key="example-run-4")["queued"])

    def test_an_agent_s_change_no_login_reaches_is_refused_before_a_person_is_asked(self):
        from hq.domains.control_plane.models import ApprovalRequest

        self.supply.reach()

        self.refused(run_payload(APP), "precondition", principal=AGENT)

        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_host_hq_does_not_know_is_refused(self):
        error = self.refused(run_payload("unknown.example.com"), "not_found")

        self.assertEqual(error["details"]["unknown"], ["unknown.example.com"])

    def test_a_procedure_switched_off_is_refused(self):
        self.ask(run_payload())
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        ManagedResource.objects.filter(key=RESTART_KEY).update(enabled=False)

        self.refused(run_payload(), "precondition", key="example-run-2")


class OneWriterTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id=LOGS))
        self.first = self.ask(run_payload(APP, DB))["operation"]

    def test_a_run_on_a_machine_another_run_holds_is_refused_naming_it(self):
        error = self.refused(run_payload(DB), "precondition", name=f"automation.{LOGS}.run", key="example-run-2")

        self.assertEqual(error["details"]["holding"]["id"], self.first["id"])
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_a_second_run_of_one_procedure_is_refused_naming_the_first(self):
        error = self.refused(run_payload(EDGE), "precondition", key="example-run-2")

        self.assertEqual(error["details"]["holding"]["id"], self.first["id"])

    def test_a_run_on_other_machines_is_queued(self):
        self.assertTrue(self.ask(run_payload(EDGE), name=f"automation.{LOGS}.run", key="example-run-2")["queued"])

    def test_a_machine_is_free_once_its_run_ends(self):
        OperationRequest.objects.update(state=OperationRequest.State.FAILED, completed_at="2026-01-01T00:00:00Z")

        self.assertTrue(self.ask(run_payload(DB), name=f"automation.{LOGS}.run", key="example-run-2")["queued"])

    def test_the_refusal_says_whether_the_playbook_or_a_machine_is_held(self):
        machine = self.refused(run_payload(DB), "precondition", name=f"automation.{LOGS}.run", key="example-run-2")
        playbook = self.refused(run_payload(EDGE), "precondition", key="example-run-3")

        self.assertEqual((machine["details"]["shared"], machine["details"]["machines"]), ([DB], sorted((APP, DB))))
        self.assertEqual(playbook["details"]["shared"], [])


class LapsedHoldTests(RunTestCase):
    """A run whose worker stopped renewing holds neither its playbook nor its
    machines, as the next claim would end it."""

    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id=LOGS))
        self.first = self.ask(run_payload(APP, DB))["operation"]
        claim_next_operation("example-worker", capabilities=((KIND, "run"),))
        OperationRequest.objects.filter(pk=self.first["id"]).update(lease_expires_at="2026-01-01T00:00:00Z")

    def test_its_machine_takes_another_playbook_s_run(self):
        self.assertTrue(self.ask(run_payload(DB), name=f"automation.{LOGS}.run", key="example-run-2")["queued"])

    def test_its_playbook_runs_again(self):
        self.assertTrue(self.ask(run_payload(EDGE), key="example-run-2")["queued"])

    def test_it_ends_as_stopped_early_with_its_outcome_unknown(self):
        self.ask(run_payload(EDGE), key="example-run-2")

        lapsed = OperationRequest.objects.get(pk=self.first["id"])
        self.assertEqual(lapsed.get_state_display(), "Stopped early")
        self.assertEqual(lapsed.result["reason"], "OutcomeUnknown")

    def test_a_live_lease_still_holds(self):
        OperationRequest.objects.filter(pk=self.first["id"]).update(lease_expires_at="2999-01-01T00:00:00Z")

        self.refused(run_payload(DB), "precondition", name=f"automation.{LOGS}.run", key="example-run-2")


class HeldRunPageTests(RunTestCase):
    """The Run page says which run is in the way, in words, and links to it."""

    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id=LOGS))
        self.first = self.ask(run_payload(APP, DB))["operation"]
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))
        machines = patch(
            "hq.platform.application.machines.machine_catalog", side_effect=lambda **_: self.supply.machines
        )
        machines.start()
        self.addCleanup(machines.stop)

    def post(self, name, *hosts):
        from django.urls import reverse

        return self.client.post(
            reverse("command", args=[name]),
            {"hosts": list(hosts), "parameters.service": "web", "__confirm_effect": "on", "__retry_key": name},
        )

    def test_a_held_machine_names_it_and_the_run_on_it_with_a_link(self):
        from django.urls import reverse

        page = self.post(f"automation.{LOGS}.run", DB)

        said = text(page)
        self.assertEqual(page.status_code, 409)
        self.assertNotIn("details.holding", said)
        self.assertIn(f"{DB} is busy with a run of", said)
        self.assertIn("waiting since", said)
        (holder,) = [href for words, href in links(page.content) if words == "See that run"]
        self.assertEqual(holder, reverse("jobs:run", kwargs={"pk": self.first["id"]}))
        self.assertEqual(self.client.get(holder).status_code, 200)

    def test_a_held_playbook_names_its_run_s_machines(self):
        said = text(self.post(RESTART_RUN, EDGE))

        self.assertIn(f"already has a run on {APP} and {DB}, waiting since", said)


class InvalidRunTests(RunTestCase):
    def test_a_free_execution_field_is_refused(self):
        for field in ("playbook", "inventory", "extra_vars", "limit"):
            with self.subTest(field=field):
                self.refused(run_payload(**{field: "example"}), "invalid_input")

    def test_a_free_field_inside_the_parameters_is_refused(self):
        payload = {"hosts": [APP], "parameters": {"service": "web", "inventory": "hosts.ini"}}

        self.refused(payload, "invalid_input")

    def test_a_correlation_longer_than_200_characters_is_refused(self):
        self.refused(run_payload(correlation="x" * 201), "invalid_input")
        self.assertTrue(self.ask(run_payload(correlation="x" * 200))["ok"])

    def test_a_correlation_with_spaces_or_lines_is_refused(self):
        for said in ("two words", "first\nsecond"):
            with self.subTest(said=said):
                self.refused(run_payload(correlation=said), "invalid_input")

    def test_a_run_names_at_least_one_host(self):
        self.refused({"hosts": [], "parameters": {"service": "web"}}, "invalid_input")

    def test_nothing_is_queued_for_any_of_them(self):
        self.refused(run_payload(playbook="site.yml"), "invalid_input")

        self.assertFalse(OperationRequest.objects.exists())
        self.assertFalse(ManagedResource.objects.filter(kind=KIND).exists())


class RetriedRunTests(RunTestCase):
    def test_the_same_key_returns_the_same_run(self):
        first = self.ask(run_payload())
        again = self.ask(run_payload())

        self.assertEqual(again["operation"]["id"], first["operation"]["id"])
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_the_same_key_with_other_parameters_is_a_conflict(self):
        self.ask(run_payload())

        self.refused(run_payload(service="db"), "idempotency_conflict")
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_a_key_outliving_its_replay_still_names_its_run(self):
        """The operation's own key holds after the replay record expires."""

        first = self.ask(run_payload())
        IdempotencyRecord.objects.all().delete()

        self.assertEqual(self.ask(run_payload())["operation"]["id"], first["operation"]["id"])
        IdempotencyRecord.objects.all().delete()
        self.refused(run_payload(service="db"), "idempotency_conflict")

    def test_the_shell_queues_without_a_key(self):
        answer = run_capability(RESTART_RUN, run_payload(), principal=cli_principal()).answer

        self.assertTrue(answer["queued"], answer)


class CapabilityShapeTests(RunTestCase):
    def test_its_effect_and_grant_are_the_manifest_s(self):
        self.supply(
            manifest(),
            manifest(id=LOGS, effect="read", retry="safe"),
            manifest(id="example.wipe", effect="destructive"),
        )
        registry = capability_registry()

        effects = {
            name: (registry[name].effect, registry[name].required_capabilities)
            for name in registry
            if name.startswith("automation.") and name.endswith(".run")
        }

        self.assertEqual(
            effects,
            {
                RESTART_RUN: ("infrastructure_change", (Capability.MANAGE_INFRASTRUCTURE,)),
                f"automation.{LOGS}.run": ("read", (Capability.READ,)),
                "automation.example.wipe.run": ("destructive", (Capability.MANAGE_INFRASTRUCTURE,)),
            },
        )

    def test_a_procedure_no_longer_supplied_is_no_longer_a_command(self):
        self.supply()

        self.assertNotIn(RESTART_RUN, capability_registry())
