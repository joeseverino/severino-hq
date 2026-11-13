"""A procedure is run through its own command, at HQ's one checkpoint, and
queued for the automation worker; nothing runs. Every manifest here is a
synthetic ``example.*`` one, supplied as an extension supplies it."""

import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.core.models import IdempotencyRecord

from ..capabilities import capability_registry, run_capability
from ..controller import claim_next_operation
from ..security import Capability, Principal, cli_principal
from .procedures import APP, DB, EDGE, RESTART, RESTART_KEY, RESTART_RUN, REVISION, Supply, manifest, run_payload

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
KIND = "automation.operation"
LOGS = "example.service_logs"


class RunTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def ask(self, payload, *, principal=AGENT, key="example-run-1", name=RESTART_RUN):
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
        self.assertEqual(readout["Playbook"], "playbooks/service_restart.yml")

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
