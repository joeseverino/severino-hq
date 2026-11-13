"""An agent's way out over HTTP: it cancels a run it asked for that nothing
has claimed, and withdraws a request it no longer wants, and no other
client can do either."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.security import web_principal
from hq.platform.application.tests.procedures import RESTART_RUN, Supply, manifest, run_payload

from .tokens import ISSUER, RESOURCE, _serving, _token

CHANGES = "read manage_infrastructure"
CANCEL = "automation.run.cancel"
WITHDRAW = "approval.withdraw"


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_SITE_HOST="hq.example.com",
)
class CancelAndWithdrawTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def call(self, name, payload, *, key, client="example-automation"):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": payload}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(client, scope=CHANGES)}",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def held(self):
        asked = self.call(RESTART_RUN, run_payload(), key="example-ask")
        self.assertEqual(asked.status_code, 202, asked.content)
        return asked.json()["data"]["approval"]["id"]

    def test_an_agent_cancels_its_approved_run_and_no_other_client_can(self):
        held_id = self.held()
        person = get_user_model().objects.create_user(username="example-owner")
        run_id = approve(ApproveCommand(held_id), principal=web_principal(person))["result"]["operation"]["id"]
        cancel = {"operation_id": run_id}

        other = self.call(CANCEL, cancel, key="example-cancel-1", client="example-other")
        self.assertEqual(other.status_code, 404, other.content)
        self.assertEqual(OperationRequest.objects.get(pk=run_id).state, OperationRequest.State.QUEUED)

        cancelled = self.call(CANCEL, cancel, key="example-cancel-2")
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(cancelled.json()["data"]["operation"]["state"], OperationRequest.State.CANCELLED)

    def test_an_agent_withdraws_its_held_request_and_no_other_client_can(self):
        held_id = self.held()
        back = {"approval_id": held_id}

        other = self.call(WITHDRAW, back, key="example-withdraw-1", client="example-other")
        self.assertEqual(other.status_code, 404, other.content)
        self.assertEqual(ApprovalRequest.objects.get(pk=held_id).state, ApprovalRequest.State.PENDING)

        withdrawn = self.call(WITHDRAW, back, key="example-withdraw-2")
        self.assertEqual(withdrawn.status_code, 200, withdrawn.content)
        self.assertEqual(ApprovalRequest.objects.get(pk=held_id).state, ApprovalRequest.State.WITHDRAWN)
