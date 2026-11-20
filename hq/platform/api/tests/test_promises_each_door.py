"""What HQ promises a caller holds on every door it reaches HQ through: the
machine API, MCP, the operator's ``hq_call`` and an extension's call
(``doors.py`` drives each through its own client)."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from itertools import combinations_with_replacement

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings

from hq.domains.assets.models import Asset
from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.domains.projects.models import Project
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.security import web_principal
from hq.platform.application.tests.procedures import APP, DB, RESTART, RESTART_RUN, Supply, manifest, run_payload
from hq.platform.application.tests.test_page_projections import page_lists, populate, served_lists
from hq.platform.core.testing import operator

from .doors import MCP, OPERATOR, SDK, Api, Caller, at_the_door, each_door

WRITER = Caller("example-agent", "read write_projects write_assets")
ASKER = Caller("example-automation", "read manage_infrastructure")
OTHER = Caller("example-other", "read manage_infrastructure")
READER = Caller("example-automation", "read")
PROJECT = {"name": "Once", "slug": "once", "status": "active"}
WORKER = "example-worker"
CLAIMS = (("automation.operation", "run"),)
LOGS = "example.service_logs"
TASK = {"host": APP, "task": "Restart the service", "outcome": "changed", "message": "Restarted."}
# The reads an agent follows a playbook's run by, and the doors that carry them;
# a supplied playbook's call is not in the committed document.
FOLLOWS = ("describe_resources", "list_resource", "get_resource", "describe_capabilities")
UNDOCUMENTED = (Api(documented=False), MCP, SDK)


class AnUpdateKeepsTests(TestCase):
    def setUp(self):
        at_the_door(self)
        Project.objects.create(name="HQ", slug="example-hq", status=Project.Status.ACTIVE, notes="Kept.")
        Asset.objects.create(item_name="Example box", slug="example-box", notes="Kept.", business_use_percentage=40)

    def test_an_update_sent_some_fields_keeps_the_rest(self):
        for caller in (WRITER, OPERATOR):
            for door in each_door(self, caller):
                with door:
                    renamed = door.capability(
                        "project.update", {"name": "Renamed", "idempotency_key": "a"}, target="example-hq"
                    )
                    costed = door.capability(
                        "asset.update", {"total_cost": "12.00", "idempotency_key": "b"}, target="example-box"
                    )

                    self.assertTrue(renamed["ok"], renamed)
                    self.assertTrue(costed["ok"], costed)
                    project = Project.objects.get(slug="example-hq")
                    self.assertEqual((project.name, project.status, project.notes), ("Renamed", "active", "Kept."))
                    asset = Asset.objects.get(slug="example-box")
                    self.assertEqual(
                        (asset.total_cost, asset.item_name, asset.notes, asset.business_use_percentage),
                        (Decimal("12.00"), "Example box", "Kept.", 40),
                    )


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class CancelAndWithdrawTests(TestCase):
    """An asker's way out: it cancels a run it asked for that nothing has
    claimed, and withdraws a request it no longer wants, and no other caller
    can do either."""

    def setUp(self):
        at_the_door(self)
        self.supply = Supply(self, manifest())
        self.person = web_principal(operator())

    def held(self, door):
        asked = door.capability(RESTART_RUN, run_payload() | {"idempotency_key": "example-ask"})
        self.assertEqual(asked.get("status"), "awaiting_approval", asked)
        return asked["approval"]["id"]

    def test_an_asker_cancels_its_approved_run_and_no_other_caller_can(self):
        for door in each_door(self, ASKER):
            with door:
                run_id = approve(ApproveCommand(self.held(door)), principal=self.person)["result"]["operation"]["id"]
                cancel = {"operation_id": run_id}

                other = door.as_(OTHER).capability("automation.run.cancel", cancel | {"idempotency_key": "c1"})
                self.assertEqual(other["error"]["code"], "not_found", other)
                self.assertEqual(OperationRequest.objects.get(pk=run_id).state, OperationRequest.State.QUEUED)

                cancelled = door.capability("automation.run.cancel", cancel | {"idempotency_key": "c2"})
                self.assertTrue(cancelled["ok"], cancelled)
                self.assertEqual(OperationRequest.objects.get(pk=run_id).state, OperationRequest.State.CANCELLED)
                self.assertIsNone(claim_next_operation(WORKER, capabilities=CLAIMS)["operation"])

    def test_an_asker_withdraws_its_held_request_and_no_other_caller_can(self):
        for door in each_door(self, ASKER):
            with door:
                held_id = self.held(door)
                back = {"approval_id": held_id}

                other = door.as_(OTHER).capability("approval.withdraw", back | {"idempotency_key": "w1"})
                self.assertEqual(other["error"]["code"], "not_found", other)
                self.assertEqual(ApprovalRequest.objects.get(pk=held_id).state, ApprovalRequest.State.PENDING)

                withdrawn = door.capability("approval.withdraw", back | {"idempotency_key": "w2"})
                self.assertTrue(withdrawn["ok"], withdrawn)
                self.assertEqual(ApprovalRequest.objects.get(pk=held_id).state, ApprovalRequest.State.WITHDRAWN)


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class ConnectionStandingTests(TestCase):
    def setUp(self):
        at_the_door(self)
        populate()

    def test_a_connections_reads_and_changes_are_the_pages(self):
        operator(self.client)
        drawn = page_lists(self.client, "example-tailnet")
        self.assertTrue(drawn[0])

        for caller in (READER, OPERATOR):
            for door in each_door(self, caller, "get_resource"):
                with door:
                    item = door("get_resource", name="connections", identifier="example-tailnet")

                    self.assertEqual(served_lists(item), drawn)


class OneRetryKeyTests(TestCase):
    def setUp(self):
        at_the_door(self)

    def test_a_retry_by_any_door_acts_once(self):
        for first in each_door(self, WRITER):
            with first:
                made = first.capability("project.create", PROJECT | {"idempotency_key": "example-once"})
                self.assertTrue(made["ok"], made)
                for again in each_door(self, WRITER):
                    with again:
                        answer = again.capability("project.create", PROJECT | {"idempotency_key": "example-once"})

                        self.assertTrue(answer["ok"], answer)
                        self.assertEqual(answer["project"]["slug"], "once")
                        self.assertEqual(Project.objects.filter(slug="once").count(), 1)


class RetryKeyRaceTests(TransactionTestCase):
    """Each write runs on a thread with a database connection of its own,
    against the WAL SQLite file production writes (``hq/platform/core/file_clones.py``)."""

    def setUp(self):
        at_the_door(self)

    def test_two_writes_at_once_by_any_two_doors_with_one_key_act_once(self):
        pairs = combinations_with_replacement(each_door(self, WRITER), 2)
        for count, pair in enumerate(pairs):
            slug, key = f"once-{count}", f"example-race-{count}"
            together = threading.Barrier(2, timeout=30)

            def write(door, slug=slug, key=key, together=together):
                try:
                    together.wait()
                    return door.capability("project.create", PROJECT | {"slug": slug, "idempotency_key": key})
                finally:
                    connection.close()

            with self.subTest(doors=[door.name for door in pair]), ThreadPoolExecutor(max_workers=2) as pool:
                answers = list(pool.map(write, pair))

                for answer in answers:
                    self.assertTrue(answer["ok"], answer)
                    self.assertEqual(answer["project"]["slug"], slug)
                self.assertEqual(Project.objects.filter(slug=slug).count(), 1)


def _named(text, among):
    """The one resource a sentence names, as a caller reads it: a quoted name that is a resource."""

    named = {word for word in re.findall(r"`([^`]+)`", text) if word in among}
    assert len(named) == 1, (text, named)
    return named.pop()


def _work(*events):
    """The automation worker's turn, through its own door: claim the queued run and report how it went."""

    claimed = claim_next_operation(WORKER, capabilities=CLAIMS)
    operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
    report = ControllerReport(
        success=True, observed_generation=operation.input["generation"], message="Restarted.", events=list(events)
    )
    report_operation(str(operation.pk), report, controller_id=WORKER)
    return str(operation.pk)


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class AutomationLoopTests(TestCase):
    """A playbook asked for the whole way round: a change is held, and its
    hold, then its run, are read by id; a read runs unheld. A caller that knows
    only the `playbooks` read finds every next call in what the answer before
    it says. A hold is its asker's alone, a caller that only reads asks no
    change, and one without the read grant reads neither runs nor holds."""

    def setUp(self):
        at_the_door(self)
        self.supply = Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))
        self.person = web_principal(operator())

    def found(self, door, effect):
        """What a caller learns before it asks: the resources HQ has, a playbook
        of ``effect`` from the `playbooks` read, and its command."""

        resources = {each["name"] for each in door("describe_resources")["resources"]}
        listed = door("list_resource", name="playbooks")["items"]
        row = next(each for each in listed if (each["effect"] == "read") == (effect == "read"))
        playbook = door("get_resource", name="playbooks", identifier=row["playbook"])
        (command,) = door("describe_capabilities", name=playbook["capability"])["capabilities"]
        return resources, playbook, command

    def test_a_held_change_is_followed_from_what_each_answer_says(self):
        for door in each_door(self, ASKER, *FOLLOWS, doors=UNDOCUMENTED):
            with door:
                resources, playbook, command = self.found(door, "infrastructure_change")
                payload = {"hosts": playbook["reachable"], "parameters": {"service": "web"}, "idempotency_key": "k"}

                held = door.capability(command["name"], payload)
                self.assertEqual(held.get("status"), "awaiting_approval", held)
                holds, held_id = _named(held["message"], resources), held["approval"]["id"]
                waiting = door("get_resource", name=holds, identifier=held_id)
                self.assertEqual((waiting["state"], waiting.get("done", False)), ("pending", False))
                approve(ApproveCommand(held_id), principal=self.person)
                _work(TASK)

                settled = door("get_resource", name=holds, identifier=held_id)
                self.assertTrue(settled["done"])
                runs = _named(" ".join(command["execution_notes"]), resources)
                run = door("get_resource", name=runs, identifier=settled["operation"])
                self.assertEqual(
                    (run["playbook"], run["state"], run["events"]), (RESTART, "succeeded", [{**TASK, "tone": "good"}])
                )

    def test_a_caller_that_only_reads_runs_a_read_unheld_from_what_each_answer_says(self):
        # The worker reaches one machine, which the read offers as the one to name.
        self.supply.reach((DB, "read"))
        for door in each_door(self, READER, *FOLLOWS, doors=UNDOCUMENTED):
            with door:
                resources, playbook, command = self.found(door, "read")
                self.assertEqual((playbook["playbook"], playbook["reachable"]), (LOGS, [DB]))

                asked = door.capability(command["name"], {"hosts": [DB], "parameters": {"service": "web"}})
                self.assertTrue(asked.get("queued"), asked)
                self.assertFalse(ApprovalRequest.objects.exists())
                runs = _named(" ".join(command["execution_notes"]), resources)
                run_id = asked["operation"]["id"]
                self.assertEqual(door("get_resource", name=runs, identifier=run_id)["state"], "queued")
                _work({**TASK, "host": DB, "outcome": "ok", "task": "Read the journal"})

                run = door("get_resource", name=runs, identifier=run_id)
                self.assertEqual((run["playbook"], run["state"], run["hosts"]), (LOGS, "succeeded", [DB]))
                self.assertEqual(run.get("approved_by", ""), "")

    def test_a_hold_is_its_asker_s_alone(self):
        for door in each_door(self, ASKER, "get_resource", doors=UNDOCUMENTED):
            with door:
                held_id = door.capability(RESTART_RUN, run_payload() | {"idempotency_key": "k"})["approval"]["id"]

                other = door.as_(OTHER)("get_resource", name="approvals", identifier=held_id)

                # Answered as an id nothing holds, naming nothing of the request.
                self.assertEqual(other["error"]["code"], "not_found", other)
                self.assertNotIn(held_id, json.dumps(other))
                self.assertEqual(door("get_resource", name="approvals", identifier=held_id)["state"], "pending")

    def test_a_caller_that_only_reads_is_refused_a_change(self):
        for door in each_door(self, READER, doors=UNDOCUMENTED):
            with door:
                refused = door.capability(RESTART_RUN, run_payload() | {"idempotency_key": "k"})

                self.assertEqual(refused["error"]["code"], "insufficient_scope", refused)
                self.assertFalse(ApprovalRequest.objects.exists())
                self.assertFalse(OperationRequest.objects.exists())

    def test_a_caller_without_the_read_grant_reads_neither_runs_nor_holds(self):
        changer = Caller(ASKER.client, "manage_infrastructure")
        for door in each_door(self, ASKER, "get_resource", doors=UNDOCUMENTED):
            with door:
                held_id = door.capability(RESTART_RUN, run_payload() | {"idempotency_key": "k"})["approval"]["id"]
                approve(ApproveCommand(held_id), principal=self.person)
                run_id = _work(TASK)

                for kind, identifier in (("runs", run_id), ("approvals", held_id)):
                    refused = door.as_(changer)("get_resource", name=kind, identifier=identifier)

                    self.assertFalse(refused["ok"], refused)
