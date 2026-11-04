"""A held change as a task of the MCP tasks extension, through the SDK's own client.

The client waits on the task while a person decides in HQ and HQ applies it:
it reads working, saying what it holds and where it is decided, then the
call's answer once the change is applied, or a refusal once a person declines
or applying it fails. Nothing in the extension decides it, and a task answers
only the caller that asked.
"""

import json
from datetime import timedelta
from typing import Any, Literal

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from mcp import Client, types
from mcp.client.extension import ClientExtension, ResultClaim
from mcp.shared.exceptions import MCPError
from pydantic import Field, TypeAdapter

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, OperationRequest
from hq.platform.application.approvals import ApproveCommand, RejectCommand, approve, asked, reject
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.hq_self import site_host
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.routes import reverse
from hq.platform.application.security import Principal, cli_principal, mcp_principal, web_principal

from ..identity import set_principal
from ..server import mcp

EXTENSION = "io.modelcontextprotocol/tasks"
POLICY_KEY = "access-policy"
CONTROLLER = "example-controller"
ANSWER = TypeAdapter(dict[str, Any])


class CreatedTask(types.Result):
    """The extension's CreateTaskResult, as its specification shapes it."""

    result_type: Literal["task"] = Field(alias="resultType")
    task_id: str
    status: str
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None
    status_message: str | None = None


async def _unresolved(_result, _context):
    raise AssertionError("These tests read the task themselves.")


class Tasks(ClientExtension):
    identifier = EXTENSION

    def claims(self):
        return (ResultClaim(result_type="task", model=CreatedTask, resolve=_unresolved),)


class TaskRequest(types.Request[types.GetTaskRequestParams, str]):
    name_param = "taskId"


def _ask(client):
    """Reconcile the access policy, which waits for a person when an agent asks."""

    return client.session.call_tool(
        "execute_capability",
        {
            "name": "infrastructure.resource.reconcile",
            "payload": {"idempotency_key": "example-once", "reason": "Stated reason for the change."},
            "target": POLICY_KEY,
        },
        allow_claimed=True,
    )


def _ask_for_a_project(client):
    """Create a project, which queues nothing, so it is done when it runs."""

    return client.session.call_tool(
        "execute_capability",
        {"name": "project.create", "payload": {"idempotency_key": "example-project", "name": "Held project"}},
        allow_claimed=True,
    )


def _apply(*, success):
    """The controller's run of the operation an approval queued, through its own door."""

    claimed = claim_next_operation(CONTROLLER)
    operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
    report_operation(
        str(operation.pk),
        ControllerReport(
            success=success,
            observed_generation=operation.input.get("generation"),
            message="Applied." if success else "The provider refused it.",
        ),
        controller_id=CONTROLLER,
    )


async def _task(client, method, task_id, **fields):
    params = types.GetTaskRequestParams(task_id=task_id)
    request = TaskRequest(method=method, params=params.model_copy(update=fields))
    return await client.session.send_request(request, ANSWER)


@override_settings(SEVERINO_AGENT_ENABLE_INFRASTRUCTURE=True, SEVERINO_SITE_HOST="hq.example.com")
class HeldTaskTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        save_managed_resource(
            ManagedResourceCommand(key=POLICY_KEY, kind="tailscale.policy", spec={"document": "{}"}),
            principal=cli_principal(),
            copied_from_live=True,
        )
        self.agent = Principal("example-agent", "mcp", mcp_principal().capabilities)
        user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        self.person = web_principal(user)

    def _session(self, steps, *, principal=None, tasks=True):
        async def run():
            async with Client(mcp.server, extensions=[Tasks()] if tasks else None) as client:
                return await steps(client)

        with set_principal(principal or self.agent):
            return async_to_sync(run)()

    def _held_task(self):
        async def steps(client):
            created = await _ask(client)
            return created, await _task(client, "tasks/get", created.task_id)

        return self._session(steps)

    def _read(self, task_id, principal=None):
        async def steps(client):
            return await _task(client, "tasks/get", task_id)

        return self._session(steps, principal=principal)

    def test_a_held_call_is_a_task_that_waits_for_a_person(self):
        created, read = self._held_task()

        held = ApprovalRequest.objects.get()
        self.assertEqual(created.task_id, str(held.pk))
        self.assertEqual(created.status, "working")
        self.assertEqual(read["status"], "working")
        self.assertNotIn("result", read)
        # Polled no faster than a person answers, within what Claude Code honors.
        self.assertLessEqual(created.poll_interval_ms, 30_000)
        self.assertGreaterEqual(created.poll_interval_ms, 250)
        self.assertFalse(OperationRequest.objects.exists())

    def test_the_task_names_what_it_holds_and_links_where_it_is_decided(self):
        created, read = self._held_task()

        held = ApprovalRequest.objects.get()
        link = f"https://{site_host()}{reverse('core:approval_entry', kwargs={'approval_id': held.pk})}"
        for answer in (created.status_message, read["statusMessage"]):
            # The sentence the decision card and the audit log say it by.
            self.assertIn(str(asked(held)), answer)
            self.assertIn(link, answer)

    def test_an_approved_change_stays_working_until_it_is_applied(self):
        created, _ = self._held_task()

        approved = approve(ApproveCommand(created.task_id), principal=self.person)
        queued = self._read(created.task_id)
        operation = OperationRequest.objects.get()
        claim_next_operation(CONTROLLER)
        applying = self._read(created.task_id)

        for read in (queued, applying):
            self.assertEqual(read["status"], "working")
            self.assertNotIn("result", read)
            self.assertIn(str(asked(ApprovalRequest.objects.get())), read["statusMessage"])
            # Read again soon, within what Claude Code honors, and never lapsed while it applies.
            self.assertLess(read["pollIntervalMs"], created.poll_interval_ms)
            self.assertGreaterEqual(read["pollIntervalMs"], 250)
            self.assertIsNone(read.get("ttlMs"))
        self.assertEqual(approved["result"]["operation"]["id"], str(operation.pk))

    def test_the_task_completes_with_the_call_answer_once_the_change_is_applied(self):
        created, _ = self._held_task()

        approved = approve(ApproveCommand(created.task_id), principal=self.person)
        _apply(success=True)
        read = self._read(created.task_id)

        self.assertEqual(read["status"], "completed")
        self.assertIn("statusMessage", read)
        result = types.CallToolResult.model_validate(read["result"])
        self.assertFalse(result.is_error)
        # The call's own answer, its operation as it ended.
        self.assertEqual(result.structured_content["operation"]["id"], approved["result"]["operation"]["id"])
        self.assertEqual(result.structured_content["operation"]["state"], OperationRequest.State.SUCCEEDED)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    def test_a_change_that_fails_to_apply_completes_as_a_tool_error(self):
        created, _ = self._held_task()

        approve(ApproveCommand(created.task_id), principal=self.person)
        _apply(success=False)
        read = self._read(created.task_id)

        # The call failed, not the protocol: completed, with the tool's error.
        self.assertEqual(read["status"], "completed")
        result = types.CallToolResult.model_validate(read["result"])
        self.assertTrue(result.is_error)
        failed = result.structured_content["error"]
        self.assertEqual(failed["details"]["operation"]["state"], OperationRequest.State.FAILED)

    def test_a_change_that_queues_nothing_completes_once_a_person_approves(self):
        CapabilityRule.objects.create(scope="surface", subject="mcp", capability="project.create", rule="approve")

        async def steps(client):
            return await _ask_for_a_project(client)

        writer = Principal(self.agent.actor, self.agent.interface, self.agent.capabilities | {"write_projects"})
        created = self._session(steps, principal=writer)
        approved = approve(ApproveCommand(created.task_id), principal=self.person)
        read = self._read(created.task_id, principal=writer)

        self.assertEqual(read["status"], "completed")
        result = types.CallToolResult.model_validate(read["result"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, approved["result"])

    def test_the_task_completes_as_a_refusal_once_a_person_declines(self):
        created, _ = self._held_task()

        reject(RejectCommand(created.task_id, note="Not this week."), principal=self.person)
        read = self._read(created.task_id)

        # A decline is the call refused, not a protocol failure.
        self.assertEqual(read["status"], "completed")
        result = types.CallToolResult.model_validate(read["result"])
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "declined")
        self.assertIn("Not this week.", result.structured_content["error"]["message"])
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_hold_nobody_decided_in_time_completes_as_expired(self):
        created, _ = self._held_task()
        ApprovalRequest.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        read = self._read(created.task_id)

        self.assertEqual(read["status"], "completed")
        result = types.CallToolResult.model_validate(read["result"])
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "expired")

    def test_another_agent_cannot_read_the_task(self):
        created, _ = self._held_task()
        other = Principal("example-other-agent", "mcp", self.agent.capabilities)

        async def steps(client):
            refused = []
            for task_id in (created.task_id, "00000000-0000-4000-8000-000000000000"):
                with self.assertRaises(MCPError) as raised:
                    await _task(client, "tasks/get", task_id)
                refused.append((raised.exception.error.code, raised.exception.error.message))
            return refused

        mine, missing = self._session(steps, principal=other)

        # Told no more than of an id nothing holds.
        self.assertEqual(mine, missing)
        self.assertEqual(mine[0], types.INVALID_PARAMS)

    def test_asking_what_another_agent_already_asked_is_a_task_of_its_own(self):
        created, _ = self._held_task()
        other = Principal("example-other-agent", "mcp", self.agent.capabilities)

        async def steps(client):
            asked = await _ask(client)
            return asked, await _task(client, "tasks/get", asked.task_id)

        answered, read = self._session(steps, principal=other)

        self.assertIsInstance(answered, CreatedTask)
        self.assertNotEqual(answered.task_id, created.task_id)
        self.assertEqual(read["status"], "working")
        self.assertEqual(ApprovalRequest.objects.get(pk=answered.task_id).requested_actor, other.actor)

    def test_nothing_in_the_extension_decides_the_hold(self):
        created, _ = self._held_task()

        async def steps(client):
            updated = await _task(client, "tasks/update", created.task_id, input_responses={"approve": {"ok": True}})
            cancelled = await _task(client, "tasks/cancel", created.task_id)
            return updated, cancelled, await _task(client, "tasks/get", created.task_id)

        _updated, cancelled, read = self._session(steps)

        # Acknowledged, as the extension requires, and the hold goes on waiting for a person.
        self.assertEqual(cancelled.keys() - {"resultType", "_meta"}, set())
        self.assertEqual(read["status"], "working")
        self.assertEqual(ApprovalRequest.objects.get().state, ApprovalRequest.State.PENDING)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_client_without_the_extension_keeps_the_held_answer(self):
        async def steps(client):
            answered = await _ask(client)
            with self.assertRaises(MCPError) as raised:
                await _task(client, "tasks/get", answered.structured_content["approval"]["id"])
            return answered, raised.exception.error.code

        answered, refused = self._session(steps, tasks=False)

        self.assertIsInstance(answered, types.CallToolResult)
        self.assertEqual(answered.structured_content["status"], "awaiting_approval")
        self.assertEqual(refused, -32021)
