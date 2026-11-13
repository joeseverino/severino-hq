"""A playbook asked for through MCP, the whole way round, through the SDK's own
client: a change is held as a task, a person approves it, the automation
worker claims and reports it, and the task completes with the run as it
ended, which the agent then reads by its id; a read runs unheld, and the
agent reads its run the same way. An agent that knows only the `playbooks`
read finds every next call in what the answer before it says."""

import re

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from mcp import Client, types

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.security import Capability, Principal, web_principal
from hq.platform.application.tests.procedures import APP, DB, RESTART, RESTART_RUN, Supply, manifest, run_payload

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


async def _answer(client, name, arguments):
    answer = await _call(client, name, arguments)
    assert not answer.is_error, answer
    return answer.structured_content


def _named(text, among):
    """The one resource a sentence names, as an agent reads it: a quoted name that is a resource."""

    named = {word for word in re.findall(r"`([^`]+)`", text) if word in among}
    assert len(named) == 1, (text, named)
    return named.pop()


async def _found(client, effect):
    """What an agent learns before it asks: the resources HQ has, a playbook
    of ``effect`` from the `playbooks` read and its command's notes."""

    resources = {each["name"] for each in (await _answer(client, "describe_resources", {}))["resources"]}
    listed = (await _answer(client, "list_resource", {"name": "playbooks"}))["items"]
    row = next(each for each in listed if (each["effect"] == "read") == (effect == "read"))
    playbook = await _answer(client, "get_resource", {"name": "playbooks", "identifier": row["playbook"]})
    described = await _answer(client, "describe_capabilities", {"name": playbook["capability"]})
    (command,) = described["capabilities"]
    return resources, playbook, command


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

    def _plain(self, steps):
        """``steps`` through a client that declares no tasks extension, so a held call answers as held."""

        async def run():
            async with Client(mcp.server) as client:
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

    def test_an_agent_runs_a_read_from_what_each_answer_says(self):
        # The worker reaches one machine, which the read offers as the one to name.
        self.supply.reach((DB, "read"))

        async def steps(client):
            resources, playbook, command = await _found(client, "read")
            asked = await _answer(
                client,
                "execute_capability",
                {
                    "name": command["name"],
                    "payload": {"hosts": playbook["reachable"], "parameters": {"service": "web"}},
                },
            )
            runs = _named(" ".join(command["execution_notes"]), resources)
            return playbook, asked, runs

        playbook, asked, runs = self._session(steps)

        self.assertEqual((playbook["playbook"], playbook["reachable"]), (LOGS, [DB]))
        self.assertTrue(asked["queued"], asked)
        _work({**TASK, "host": DB, "outcome": "ok", "task": "Read the journal"})

        async def read(client):
            return await _answer(client, "get_resource", {"name": runs, "identifier": asked["operation"]["id"]})

        run = self._session(read)
        self.assertEqual((run["playbook"], run["state"], run["hosts"]), (LOGS, "succeeded", [DB]))

    def test_an_agent_without_tasks_follows_a_held_change_from_what_each_answer_says(self):
        async def ask(client):
            resources, playbook, command = await _found(client, "infrastructure_change")
            payload = {"hosts": playbook["reachable"], "parameters": {"service": "web"}, "idempotency_key": "example"}
            held = await _answer(client, "execute_capability", {"name": command["name"], "payload": payload})
            holds = _named(held["message"], resources)
            waiting = await _answer(client, "get_resource", {"name": holds, "identifier": held["approval"]["id"]})
            return resources, command, held, holds, waiting

        resources, command, held, holds, waiting = self._plain(ask)

        self.assertEqual((held["status"], waiting["state"]), ("awaiting_approval", "pending"))
        approve(ApproveCommand(held["approval"]["id"]), principal=self.person)
        _work(TASK)

        async def follow(client):
            settled = await _answer(client, "get_resource", {"name": holds, "identifier": held["approval"]["id"]})
            runs = _named(" ".join(command["execution_notes"]), resources)
            return await _answer(client, "get_resource", {"name": runs, "identifier": settled["operation"]})

        run = self._plain(follow)
        self.assertEqual((run["playbook"], run["state"], run["events"]), (RESTART, "succeeded", [TASK]))
