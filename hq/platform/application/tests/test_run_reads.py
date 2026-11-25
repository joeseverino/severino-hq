"""A playbook's run is read by the operation id an answer carried, under the
reader's own read grant, showing what Jobs shows of it and nothing more."""

from django.test import TestCase

from hq.domains.control_plane.models import OperationRequest
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import AGENT, MCP_READER, run_ended

from ..controller import claim_next_operation
from ..record_errors import RecordNotFound
from ..resources import get_resource
from ..security import AuthorizationError, Capability, Principal
from .procedures import APP, RESTART, Supply, approved_run, manifest, run_payload

# A grant that reads the calendar and nothing of the estate.
UNREAD = Principal("example-calendar", "mcp", frozenset({Capability.READ_CALENDAR}))
WORKER = "example-worker"
RUNS = "runs"


class ReadOneRunTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def asked(self, *hosts, correlation=""):
        answer = approved_run(
            self,
            run_payload(*hosts, correlation=correlation),
            principal=AGENT,
            key="example-run",
            name=f"automation.{RESTART}.run",
        )
        return answer["operation"]["id"]

    def finish(self, operation_id, *events):
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        run_ended(operation_id, WORKER, events=events, success=True, message="Restarted.")

    def test_a_run_is_read_by_the_operation_id_its_answer_carried(self):
        operation_id = self.asked(APP, correlation="example-investigation-7")
        task = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}
        self.finish(operation_id, task)

        run = get_resource(RUNS, operation_id, principal=MCP_READER)

        self.assertEqual(run["operation_id"], operation_id)
        self.assertEqual(run["playbook"], RESTART)
        self.assertEqual(run["capability"], f"automation.{RESTART}.run")
        self.assertEqual((run["state"], run["headline"]), ("succeeded", "Restarted."))
        self.assertEqual(run["hosts"], [APP])
        self.assertEqual(run["correlation"], "example-investigation-7")
        queued = AuditLog.objects.get(object_id=operation_id, action=AuditLog.Action.CREATED)
        self.assertEqual((run["by"], run["approved_by"]), (queued.who.words, queued.who.approved_by))
        self.assertTrue(run["approved_by"])
        self.assertEqual(run["events"], [{**task, "tone": "good"}])
        self.assertIsNotNone(run["finished_at"])

    def test_a_run_shows_only_what_jobs_draws_of_its_result(self):
        operation_id = self.asked(APP)
        self.finish(operation_id)
        # A result holding more than a run's report carries, as a hand edit could leave it.
        OperationRequest.objects.filter(pk=operation_id).update(
            result={
                "message": "Restarted.",
                "conditions": [],
                "events": [{"host": APP, "task": "Restart", "outcome": "ok", "message": "", "vars": "example-secret"}],
                "inventory": {"address": "192.0.2.10"},
            }
        )

        run = get_resource(RUNS, operation_id, principal=MCP_READER)

        self.assertEqual(
            run["events"], [{"host": APP, "task": "Restart", "outcome": "ok", "message": "", "tone": "good"}]
        )
        self.assertNotIn("example-secret", str(run))
        self.assertNotIn("192.0.2.10", str(run))

    def test_a_claimed_run_names_its_worker_and_lease(self):
        operation_id = self.asked(APP)
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))

        run = get_resource(RUNS, operation_id, principal=MCP_READER)

        self.assertEqual((run["state"], run["worker"], run["headline"]), ("running", WORKER, ""))
        self.assertIsNotNone(run["lease_until"])

    def test_a_scan_of_the_source_is_read_as_a_run_of_no_playbook(self):
        scan = OperationRequest.objects.create(
            resource=OperationRequest.objects.get(pk=self.asked(APP)).resource,
            action=OperationRequest.Action.SCAN,
            requested_actor="example-operator",
            requested_interface="web",
        )

        run = get_resource(RUNS, str(scan.pk), principal=MCP_READER)

        self.assertEqual((run["operation_id"], run["playbook"]), (str(scan.pk), ""))

    def test_an_id_no_run_holds_is_not_found(self):
        # An operation of the controller's, which no worker of playbooks runs.
        applied = OperationRequest.objects.create(
            resource=OperationRequest.objects.get(pk=self.asked(APP)).resource,
            action=OperationRequest.Action.RECONCILE,
            requested_actor="example-operator",
            requested_interface="web",
        )
        for missing in ("00000000-0000-4000-8000-000000000000", "not-an-id", str(applied.pk)):
            with self.subTest(missing=missing), self.assertRaises(RecordNotFound):
                get_resource(RUNS, missing, principal=MCP_READER)

    def test_a_reader_without_the_read_grant_is_refused(self):
        operation_id = self.asked(APP)

        with self.assertRaises(AuthorizationError):
            get_resource(RUNS, operation_id, principal=UNREAD)
