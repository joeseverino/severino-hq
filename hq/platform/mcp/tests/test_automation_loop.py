"""A playbook asked for through MCP's tasks extension, through the SDK's own
client: a change is held as a task, a person approves it, the automation
worker claims and reports it, and the task completes with the run as it
ended, which the agent then reads by its id. What every door promises of a
playbook's run is ``hq/platform/api/tests/test_promises_each_door.py``'s."""

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
TASK = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class AutomationLoopTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def _session(self, steps):
        async def run():
            async with Client(mcp.server, extensions=[Tasks()]) as client:
                return await steps(client)

        with set_principal(AGENT):
            return async_to_sync(run)()

    def test_a_change_is_held_approved_claimed_and_reported_and_its_task_completes(self):
        async def ask(client):
            payload = run_payload(correlation="example-loop") | {"idempotency_key": "example-loop"}
            return await client.session.call_tool(
                "execute_capability", {"name": RESTART_RUN, "payload": payload}, allow_claimed=True
            )

        created = self._session(ask)
        held = ApprovalRequest.objects.get()
        self.assertEqual((created.task_id, created.status), (str(held.pk), "working"))
        self.assertFalse(OperationRequest.objects.exists())

        approve(ApproveCommand(created.task_id), principal=web_principal(get_user_model().objects.create_user("o")))
        claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
        report = ControllerReport(
            success=True, observed_generation=operation.input["generation"], message="Restarted.", events=[TASK]
        )
        report_operation(str(operation.pk), report, controller_id=WORKER)

        async def read(client):
            done = await _task(client, "tasks/get", created.task_id)
            run = await client.session.call_tool(
                "get_resource", {"name": "runs", "identifier": str(operation.pk)}, allow_claimed=True
            )
            return done, run.structured_content

        done, run = self._session(read)
        self.assertEqual(done["status"], "completed")
        result = types.CallToolResult.model_validate(done["result"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["operation"]["id"], str(operation.pk))
        self.assertEqual((run["playbook"], run["state"], run["correlation"]), (RESTART, "succeeded", "example-loop"))
        self.assertEqual(run["events"], [TASK])
        self.assertTrue(run["approved_by"])
