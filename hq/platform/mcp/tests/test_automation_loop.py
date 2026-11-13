"""A playbook asked for through MCP, the whole way round, through the SDK's own
client: a change is held as a task, a person approves it, the automation
worker claims and reports it, and the task completes with the run as it
ended, which the agent then reads by its id; a read runs unheld, and the
agent reads its run the same way."""

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from mcp import Client, types

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.security import Capability, Principal, web_principal
from hq.platform.application.tests.procedures import APP, RESTART, RESTART_RUN, Supply, manifest, run_payload

from ..identity import set_principal
from ..server import mcp
from .test_held_tasks import Tasks, _task

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
WORKER = "example-worker"
LOGS = "example.service_logs"
TASK = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}


def _work(*events):
    """The automation worker's turn, through its own door: claim the queued run and report how it went."""

    claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
    operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
    report_operation(
        str(operation.pk),
        ControllerReport(
            success=True,
            observed_generation=operation.input["generation"],
            message="Restarted.",
            events=list(events),
        ),
        controller_id=WORKER,
    )
    return str(operation.pk)


def _call(client, name, arguments):
    return client.session.call_tool(name, arguments, allow_claimed=True)


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class AutomationLoopTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))
        self.person = web_principal(get_user_model().objects.create_user(username="example-owner"))

    def _session(self, steps):
        async def run():
            async with Client(mcp.server, extensions=[Tasks()]) as client:
                return await steps(client)

        with set_principal(AGENT):
            return async_to_sync(run)()

    def _run(self, run_id):
        async def steps(client):
            return await _call(client, "get_resource", {"name": "runs", "identifier": run_id})

        return self._session(steps).structured_content

    def test_a_change_is_held_approved_claimed_and_reported_and_its_task_completes(self):
        async def ask(client):
            payload = run_payload(correlation="example-loop") | {"idempotency_key": "example-loop"}
            return await _call(client, "execute_capability", {"name": RESTART_RUN, "payload": payload})

        created = self._session(ask)
        held = ApprovalRequest.objects.get()
        self.assertEqual((created.task_id, created.status), (str(held.pk), "working"))
        self.assertFalse(OperationRequest.objects.exists())

        approve(ApproveCommand(created.task_id), principal=self.person)
        run_id = _work(TASK)

        async def read(client):
            return await _task(client, "tasks/get", created.task_id)

        done = self._session(read)
        self.assertEqual(done["status"], "completed")
        result = types.CallToolResult.model_validate(done["result"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["operation"]["id"], run_id)
        run = self._run(run_id)
        self.assertEqual((run["playbook"], run["state"], run["correlation"]), (RESTART, "succeeded", "example-loop"))
        self.assertEqual(run["events"], [TASK])
        self.assertTrue(run["approved_by"])

    def test_a_read_runs_unheld_and_its_run_is_read_by_its_id(self):
        async def ask(client):
            return await _call(
                client, "execute_capability", {"name": f"automation.{LOGS}.run", "payload": run_payload()}
            )

        answer = self._session(ask)

        self.assertFalse(answer.is_error, answer)
        self.assertTrue(answer.structured_content["queued"])
        self.assertFalse(ApprovalRequest.objects.exists())
        run_id = answer.structured_content["operation"]["id"]
        self.assertEqual(self._run(run_id)["state"], "queued")
        _work({**TASK, "outcome": "ok", "task": "Read the journal"})
        run = self._run(run_id)
        self.assertEqual((run["playbook"], run["state"], run["approved_by"]), (LOGS, "succeeded", ""))
        self.assertEqual(run["events"][0]["task"], "Read the journal")
