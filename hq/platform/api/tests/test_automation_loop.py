"""A playbook asked for over HTTP, the whole way round: a change answers 202
with its held request, which its asker alone reads by id; once a person
approves it and the worker reports it, the hold names its run and the run
is read by its id. A token that only reads is refused a change and runs a
read, and one without the read grant reads neither runs nor holds. A client
that knows only the `playbooks` read finds every next call in what the
answer before it says."""

import json
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.security import web_principal
from hq.platform.application.tests.procedures import APP, DB, RESTART, RESTART_RUN, Supply, manifest, run_payload

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

WORKER = "example-worker"
LOGS = "example.service_logs"
CHANGES = "read manage_infrastructure"
TASK = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}


def _named(text, among):
    """The one resource a sentence names, as a client reads it: a quoted name that is a resource."""

    named = {word for word in re.findall(r"`([^`]+)`", text) if word in among}
    assert len(named) == 1, (text, named)
    return named.pop()


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

    def ask(self, name, *, scope=CHANGES, key="example-loop", client="example-automation", payload=None):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": payload or run_payload()}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(client, scope=scope)}",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def read(self, kind, identifier, *, scope="read", client="example-automation"):
        with _serving():
            return self.reads.get(
                f"/api/v2/resources/{kind}/{identifier}/", HTTP_AUTHORIZATION=f"Bearer {_token(client, scope=scope)}"
            )

    def got(self, path, *, client=None, scope=CHANGES):
        """The data of a read at ``path`` under /api/v2/, which must succeed."""

        with _serving():
            answer = (client or self.reads).get(
                f"/api/v2/{path}", HTTP_AUTHORIZATION=f"Bearer {_token('example-automation', scope=scope)}"
            )
        self.assertEqual(answer.status_code, 200, answer.content)
        return answer.json()["data"]

    def found(self, effect, *, scope=CHANGES):
        """What a client learns before it asks: the resources HQ has, a
        playbook of ``effect`` from the `playbooks` read and its command."""

        resources = {each["name"] for each in self.got("resources/", scope=scope)["resources"]}
        row = next(
            each
            for each in self.got("resources/playbooks/", scope=scope)["items"]
            if (each["effect"] == "read") == (effect == "read")
        )
        playbook = self.got(f"resources/playbooks/{row['playbook']}/", scope=scope)
        # A playbook's command is documented from the catalog adopted, which these synthetic ones are not.
        catalog = self.got(f"capabilities/?name={playbook['capability']}", client=self.client, scope=scope)
        (command,) = catalog["capabilities"]
        return resources, playbook, command

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

    def test_a_read_token_runs_a_read_from_what_each_answer_says(self):
        # The worker reaches one machine, which the read offers as the one to name.
        self.supply.reach((DB, "read"))
        resources, playbook, command = self.found("read", scope="read")
        self.assertEqual(playbook["reachable"], [DB])

        payload = {"hosts": playbook["reachable"], "parameters": {"service": "web"}}
        ran = self.ask(command["name"], scope="read", key="example-read", payload=payload)

        self.assertEqual(ran.status_code, 200, ran.content)
        runs = _named(" ".join(command["execution_notes"]), resources)
        run = self.got(f"resources/{runs}/{ran.json()['data']['operation']['id']}/", scope="read")
        self.assertEqual((run["playbook"], run["state"], run["hosts"]), (LOGS, "queued", [DB]))

    def test_a_held_change_is_followed_from_what_each_answer_says(self):
        resources, playbook, command = self.found("infrastructure_change")
        payload = {"hosts": playbook["reachable"], "parameters": {"service": "web"}}

        asked = self.ask(command["name"], payload=payload)

        self.assertEqual(asked.status_code, 202, asked.content)
        held = asked.json()["data"]
        holds = _named(held["message"], resources)
        self.assertEqual(self.got(f"resources/{holds}/{held['approval']['id']}/")["state"], "pending")
        approve(
            ApproveCommand(held["approval"]["id"]),
            principal=web_principal(get_user_model().objects.create_user(username="example-owner")),
        )
        self.work()
        settled = self.got(f"resources/{holds}/{held['approval']['id']}/")
        runs = _named(" ".join(command["execution_notes"]), resources)
        run = self.got(f"resources/{runs}/{settled['operation']}/")
        self.assertEqual((run["playbook"], run["state"], run["events"]), (RESTART, "succeeded", [TASK]))
