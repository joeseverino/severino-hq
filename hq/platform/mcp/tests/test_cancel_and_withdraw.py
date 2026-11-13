"""An agent's way out over MCP, through the SDK's own client: it cancels a
run it asked for before the worker claims it, and withdraws a request it no
longer wants (``test_held_tasks`` holds the same through ``tasks/cancel``)."""

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from mcp import Client

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import claim_next_operation
from hq.platform.application.security import Capability, Principal, web_principal
from hq.platform.application.tests.procedures import RESTART_RUN, Supply, manifest, run_payload

from ..identity import set_principal
from ..server import mcp
from .test_held_tasks import Tasks

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
CANCEL = "automation.run.cancel"
CLAIMS = (("automation.operation", "run"),)


def _call(client, name, arguments):
    return client.session.call_tool(name, arguments, allow_claimed=True)


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class CancelAndWithdrawTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())
        self.person = web_principal(get_user_model().objects.create_user(username="example-owner"))

    def session(self, steps):
        async def run():
            async with Client(mcp.server, extensions=[Tasks()]) as client:
                return await steps(client)

        with set_principal(AGENT):
            return async_to_sync(run)()

    def capability(self, name, payload):
        async def steps(client):
            return await _call(client, "execute_capability", {"name": name, "payload": payload})

        return self.session(steps)

    def test_an_agent_cancels_its_approved_run_before_the_worker_claims_it(self):
        created = self.capability(RESTART_RUN, run_payload() | {"idempotency_key": "example-asked"})
        approve(ApproveCommand(created.task_id), principal=self.person)
        run_id = str(OperationRequest.objects.get().pk)

        answer = self.capability(CANCEL, {"operation_id": run_id, "idempotency_key": "example-cancel"})

        self.assertFalse(answer.is_error, answer)
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CANCELLED)
        self.assertIsNone(claim_next_operation("example-worker", capabilities=CLAIMS)["operation"])

    def test_an_agent_withdraws_its_held_request_by_its_id(self):
        created = self.capability(RESTART_RUN, run_payload() | {"idempotency_key": "example-asked"})

        answer = self.capability("approval.withdraw", {"approval_id": created.task_id, "idempotency_key": "back"})

        self.assertFalse(answer.is_error, answer)
        self.assertEqual(ApprovalRequest.objects.get().state, ApprovalRequest.State.WITHDRAWN)
