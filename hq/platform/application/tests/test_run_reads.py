"""A playbook's runs are read by the operation id an answer carried, or listed
by playbook, machine, correlation id and state, under the reader's own read
grant, showing what Jobs shows of each run and nothing more."""

from django.test import TestCase

from hq.domains.control_plane.models import OperationRequest

from ..controller import ControllerReport, claim_next_operation, report_operation
from ..record_errors import RecordNotFound
from ..resources import get_resource, list_resource
from ..security import AuthorizationError, Capability, Principal
from .procedures import APP, DB, RESTART, Supply, approved_run, manifest, run_payload

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
# A grant that reads the calendar and nothing of the estate.
UNREAD = Principal("example-calendar", "mcp", frozenset({Capability.READ_CALENDAR}))
WORKER = "example-worker"
LOGS = "example.service_logs"
RUNS = "runs"


class RunReadTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))

    def asked(self, *hosts, correlation="", key="example-run"):
        answer = approved_run(
            self,
            run_payload(*hosts, correlation=correlation),
            principal=AGENT,
            key=key,
            name=f"automation.{RESTART}.run",
        )
        return answer["operation"]["id"]

    def finish(self, operation_id, *events, success=True):
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        operation = OperationRequest.objects.get(pk=operation_id)
        report_operation(
            operation_id,
            ControllerReport(
                success=success,
                observed_generation=operation.input["generation"],
                message="Restarted." if success else "It stayed down.",
                events=list(events),
            ),
            controller_id=WORKER,
        )


class ReadOneRunTests(RunReadTestCase):
    def test_a_run_is_read_by_the_operation_id_its_answer_carried(self):
        operation_id = self.asked(APP, correlation="example-investigation-7")
        task = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}
        self.finish(operation_id, task)

        run = get_resource(RUNS, operation_id, principal=READER)

        self.assertEqual(run["operation_id"], operation_id)
        self.assertEqual(run["playbook"], RESTART)
        self.assertEqual(run["capability"], f"automation.{RESTART}.run")
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(run["hosts"], [APP])
        self.assertEqual(run["correlation"], "example-investigation-7")
        self.assertEqual(run["by"], "example-agent (agent)")
        self.assertTrue(run["approved_by"])
        self.assertEqual(run["events"], [task])
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

        run = get_resource(RUNS, operation_id, principal=READER)

        self.assertEqual(run["events"], [{"host": APP, "task": "Restart", "outcome": "ok", "message": ""}])
        self.assertNotIn("example-secret", str(run))
        self.assertNotIn("192.0.2.10", str(run))

    def test_a_claimed_run_names_its_worker_and_lease(self):
        operation_id = self.asked(APP)
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))

        run = get_resource(RUNS, operation_id, principal=READER)

        self.assertEqual((run["state"], run["worker"]), ("running", WORKER))
        self.assertIsNotNone(run["lease_until"])

    def test_an_id_no_run_holds_is_not_found(self):
        scan = OperationRequest.objects.create(
            resource=OperationRequest.objects.get(pk=self.asked(APP)).resource,
            action=OperationRequest.Action.SCAN,
            requested_actor="example-operator",
            requested_interface="web",
        )
        for missing in ("00000000-0000-4000-8000-000000000000", "not-an-id", str(scan.pk)):
            with self.subTest(missing=missing), self.assertRaises(RecordNotFound):
                get_resource(RUNS, missing, principal=READER)

    def test_a_reader_without_the_read_grant_is_refused(self):
        operation_id = self.asked(APP)

        with self.assertRaises(AuthorizationError):
            get_resource(RUNS, operation_id, principal=UNREAD)
        with self.assertRaises(AuthorizationError):
            list_resource(RUNS, {}, principal=UNREAD)


class ListRunsTests(RunReadTestCase):
    def ids(self, query):
        return [item["operation_id"] for item in list_resource(RUNS, query, principal=READER)["items"]]

    def test_runs_are_listed_by_playbook_machine_correlation_and_state(self):
        first = self.asked(APP, correlation="example-first", key="example-first")
        self.finish(first, success=False)
        second = self.asked(DB, correlation="example-second", key="example-second")

        self.assertEqual(self.ids({}), [second, first])
        self.assertEqual(self.ids({"playbook": RESTART}), [second, first])
        self.assertEqual(self.ids({"playbook": LOGS}), [])
        self.assertEqual(self.ids({"machine": APP}), [first])
        self.assertEqual(self.ids({"correlation": "example-second"}), [second])
        self.assertEqual(self.ids({"state": "failed"}), [first])
        self.assertEqual(self.ids({"state": "queued", "machine": DB}), [second])

    def test_every_run_is_reached_page_by_page(self):
        first = self.asked(APP, key="example-first")
        self.finish(first)
        second = self.asked(APP, key="example-second")

        page = list_resource(RUNS, {"limit": 1}, principal=READER)
        after = list_resource(RUNS, {"limit": 1, "cursor": page["next"]["cursor"]}, principal=READER)

        self.assertEqual([item["operation_id"] for item in page["items"] + after["items"]], [second, first])
        self.assertIsNone(after["next"])

    def test_a_filter_it_does_not_take_is_refused_naming_it(self):
        from ..resources import InvalidResourceInput

        for query in (
            {"state": "example-state"},
            {"host": APP},
            {"playbook": "not a playbook"},
            {"correlation": "a b"},
        ):
            with self.subTest(query=query), self.assertRaises(InvalidResourceInput) as refused:
                list_resource(RUNS, query, principal=READER)
            self.assertEqual(refused.exception.details["errors"][0]["loc"][0], next(iter(query)))
