"""A playbook asked for over HTTP, the whole way round: a change answers 202
with its held request, which its asker alone reads by id; once a person
approves it and the worker reports it, the hold names its run and the run
is read by its id. A token that only reads is refused a change and runs a
read, and one without the read grant reads neither runs nor holds."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.security import web_principal
from hq.platform.application.tests.procedures import APP, RESTART, RESTART_RUN, Supply, manifest, run_payload

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

WORKER = "example-worker"
LOGS = "example.service_logs"
CHANGES = "read manage_infrastructure"
TASK = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_SITE_HOST="hq.example.com",
)
class AutomationLoopTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))
        # Each read is held to the committed document. A playbook's own command
        # is documented from the catalog adopted, which these synthetic ones are
        # not, so it is asked through the plain client.
        self.reads = ContractClient()

    def ask(self, name, *, scope=CHANGES, key="example-loop", client="example-automation"):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": run_payload()}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(client, scope=scope)}",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def read(self, kind, identifier, *, scope="read", client="example-automation"):
        with _serving():
            return self.reads.get(
                f"/api/v2/resources/{kind}/{identifier}/", HTTP_AUTHORIZATION=f"Bearer {_token(client, scope=scope)}"
            )

    def work(self):
        claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
        report_operation(
            str(operation.pk),
            ControllerReport(
                success=True, observed_generation=operation.input["generation"], message="Restarted.", events=[TASK]
            ),
            controller_id=WORKER,
        )
        return str(operation.pk)

    def test_a_change_is_held_and_its_hold_then_its_run_are_read_by_id(self):
        asked = self.ask(RESTART_RUN)
        self.assertEqual(asked.status_code, 202, asked.content)
        held_id = asked.json()["data"]["approval"]["id"]

        waiting = self.read("approvals", held_id)
        self.assertEqual(waiting.status_code, 200, waiting.content)
        self.assertEqual((waiting.json()["data"]["state"], waiting.json()["data"]["done"]), ("pending", False))

        person = get_user_model().objects.create_user(username="example-owner")
        approve(ApproveCommand(held_id), principal=web_principal(person))
        run_id = self.work()

        settled = self.read("approvals", held_id).json()["data"]
        self.assertTrue(settled["done"])
        self.assertEqual(settled["operation"], run_id)
        run = self.read("runs", run_id)
        self.assertEqual(run.status_code, 200, run.content)
        found = run.json()["data"]
        self.assertEqual((found["playbook"], found["state"], found["events"]), (RESTART, "succeeded", [TASK]))

    def test_a_hold_is_its_asker_s_alone(self):
        held_id = self.ask(RESTART_RUN).json()["data"]["approval"]["id"]

        other = self.read("approvals", held_id, client="example-other")

        # Answered as an id nothing holds, naming nothing of the request.
        self.assertEqual(other.status_code, 404, other.content)
        self.assertEqual(other.json()["error"]["code"], "not_found")
        self.assertNotIn(held_id, other.content.decode())
        self.assertEqual(self.read("approvals", held_id).status_code, 200)

    def test_a_token_that_only_reads_is_refused_a_change_and_runs_a_read(self):
        refused = self.ask(RESTART_RUN, scope="read")
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertFalse(OperationRequest.objects.exists())

        ran = self.ask(f"automation.{LOGS}.run", scope="read", key="example-read")
        self.assertEqual(ran.status_code, 200, ran.content)
        run_id = ran.json()["data"]["operation"]["id"]
        run = self.read("runs", run_id)
        self.assertEqual(run.status_code, 200, run.content)
        self.assertEqual(run.json()["data"]["playbook"], LOGS)

    def test_a_token_without_the_read_grant_reads_neither_runs_nor_holds(self):
        held_id = self.ask(RESTART_RUN).json()["data"]["approval"]["id"]
        approve(ApproveCommand(held_id), principal=web_principal(get_user_model().objects.create_user(username="o")))
        run_id = self.work()

        for kind, identifier in (("runs", run_id), ("approvals", held_id)):
            with self.subTest(kind=kind):
                refused = self.read(kind, identifier, scope="manage_infrastructure")
                self.assertEqual(refused.status_code, 403, refused.content)
