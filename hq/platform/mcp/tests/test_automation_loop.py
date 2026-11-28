"""A playbook asked for through MCP's tasks extension, through the SDK's own
client: a run is a task the agent waits on until it ends, and a change held
for a person is one task across the hold and the run its approval queued.
What every door promises of a playbook's run is
``hq/platform/api/tests/test_promises_each_door.py``'s."""

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from mcp import Client, types
from mcp.shared.exceptions import MCPError

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import claim_next_operation
from hq.platform.application.security import Principal, web_principal
from hq.platform.application.tests.procedures import APP, RESTART, RESTART_RUN, Supply, manifest, run_payload
from hq.platform.core.testing import AGENT, operator, run_ended

from ..identity import set_principal
from ..server import mcp
from .test_held_tasks import CreatedTask, Tasks, _task

WORKER = "example-worker"
LOGS = "example.service_logs"
TASK = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted.", "warnings": ""}


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class AutomationLoopTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))

    def _session(self, steps, *, tasks=True, principal=AGENT):
        async def run():
            async with Client(mcp.server, extensions=[Tasks()] if tasks else None) as client:
                return await steps(client)

        with set_principal(principal):
            return async_to_sync(run)()

    def _read_logs(self, *, tasks=True):
        """Ask for the read playbook's run, which nothing holds."""

        async def ask(client):
            payload = run_payload(correlation="example-logs")
            return await client.session.call_tool(
                "execute_capability", {"name": f"automation.{LOGS}.run", "payload": payload}, allow_claimed=True
            )

        return self._session(ask, tasks=tasks)

    def _get(self, task_id):
        async def read(client):
            return await _task(client, "tasks/get", task_id)

        return self._session(read)

    def _claimed(self):
        claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        return OperationRequest.objects.get(pk=claimed["operation"]["id"])

    def test_a_run_is_a_task_that_works_until_it_ends_and_completes_with_the_run(self):
        created = self._read_logs()
        operation = OperationRequest.objects.get()
        waiting = self._get(created.task_id)
        self._claimed()
        running = self._get(created.task_id)
        run_ended(str(operation.pk), WORKER, events=[TASK], success=True, message="Read.")
        done = self._get(created.task_id)

        self.assertIsInstance(created, CreatedTask)
        self.assertEqual((created.task_id, created.status), (str(operation.pk), "working"))
        for read in (waiting, running):
            self.assertEqual(read["status"], "working")
            self.assertNotIn("result", read)
            self.assertTrue(read["statusMessage"])
            self.assertGreaterEqual(read["pollIntervalMs"], 250)
        self.assertEqual(done["status"], "completed")
        result = types.CallToolResult.model_validate(done["result"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["operation"]["id"], str(operation.pk))
        self.assertEqual(result.structured_content["operation"]["state"], OperationRequest.State.SUCCEEDED)

    def test_a_run_that_fails_completes_as_a_tool_error(self):
        created = self._read_logs()
        operation = self._claimed()
        run_ended(str(operation.pk), WORKER, success=False, message="The service is not installed.")
        done = self._get(created.task_id)

        # The run failed, not the protocol: completed, with the tool's error.
        self.assertEqual(done["status"], "completed")
        result = types.CallToolResult.model_validate(done["result"])
        self.assertTrue(result.is_error)
        failed = result.structured_content["error"]
        self.assertEqual(failed["code"], "operation_failed")
        self.assertEqual(failed["details"]["operation"]["state"], OperationRequest.State.FAILED)

    def test_a_cancel_before_the_runner_starts_it_cancels_the_run(self):
        created = self._read_logs()

        async def cancel(client):
            cancelled = await _task(client, "tasks/cancel", created.task_id)
            return cancelled, await _task(client, "tasks/get", created.task_id)

        cancelled, read = self._session(cancel)

        self.assertEqual(cancelled.keys() - {"resultType", "_meta"}, set())
        self.assertEqual(read["status"], "cancelled")
        self.assertNotIn("result", read)
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CANCELLED)

    def test_a_cancel_once_the_runner_started_it_leaves_the_run_to_end(self):
        created = self._read_logs()
        self._claimed()

        async def cancel(client):
            await _task(client, "tasks/cancel", created.task_id)
            return await _task(client, "tasks/get", created.task_id)

        self.assertEqual(self._session(cancel)["status"], "working")
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CLAIMED)

    def test_another_agent_cannot_read_or_cancel_the_run_task(self):
        created = self._read_logs()
        other = Principal("example-other-agent", "mcp", AGENT.capabilities)

        async def steps(client):
            codes = []
            for method in ("tasks/get", "tasks/cancel"):
                with self.assertRaises(MCPError) as raised:
                    await _task(client, method, created.task_id)
                codes.append(raised.exception.error.code)
            return codes

        self.assertEqual(self._session(steps, principal=other), [types.INVALID_PARAMS] * 2)
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.QUEUED)

    def test_a_client_without_the_extension_keeps_the_run_answer(self):
        answered = self._read_logs(tasks=False)

        self.assertIsInstance(answered, types.CallToolResult)
        self.assertEqual(answered.structured_content["operation"]["id"], str(OperationRequest.objects.get().pk))

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

        approve(ApproveCommand(created.task_id), principal=web_principal(operator()))
        claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
        run_ended(str(operation.pk), WORKER, events=[TASK], success=True, message="Restarted.")

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
        self.assertEqual(run["events"], [{**TASK, "tone": "good"}])
        self.assertTrue(run["approved_by"])

    def test_a_cancel_of_an_approved_run_before_it_starts_cancels_the_run(self):
        async def ask(client):
            payload = run_payload() | {"idempotency_key": "example-cancel"}
            return await client.session.call_tool(
                "execute_capability", {"name": RESTART_RUN, "payload": payload}, allow_claimed=True
            )

        created = self._session(ask)
        approve(ApproveCommand(created.task_id), principal=web_principal(operator()))

        async def cancel(client):
            await _task(client, "tasks/cancel", created.task_id)
            return await _task(client, "tasks/get", created.task_id)

        read = self._session(cancel)

        self.assertEqual(read["status"], "cancelled")
        self.assertNotIn("result", read)
        self.assertEqual(ApprovalRequest.objects.get().state, ApprovalRequest.State.APPROVED)
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CANCELLED)
