"""A playbook's run nothing has claimed can be cancelled, so it holds its
playbook and machines no more: by a person with the run's own authority,
from where the run is listed or from the refusal it caused, and by an agent
for a run it asked for, over the same command."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.core.models import AuditLog

from ..capabilities import run_capability
from ..controller import claim_next_operation
from ..security import Capability, Principal, web_principal
from .markup import select, text
from .procedures import APP, DB, EDGE, RESTART_RUN, Supply, approved_run, manifest, run_payload

CANCEL = "automation.run.cancel"
OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
VIEWER = Principal("example-viewer", "web", frozenset({Capability.READ}))
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
OTHER_AGENT = Principal("example-other-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
LOGS = "example.service_logs"
LOGS_RUN = f"automation.{LOGS}.run"
WORKER = "example-worker"


class CancelTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS))
        self.keys = iter(range(1000))

    def ask(self, *hosts, principal=OPERATOR, name=RESTART_RUN):
        answer = run_capability(
            name, run_payload(*hosts), principal=principal, idempotency_key=f"example-{next(self.keys)}"
        ).answer
        self.assertTrue(answer["ok"], answer)
        return answer

    def cancel(self, operation_id, *, principal=OPERATOR):
        return run_capability(
            CANCEL,
            {"operation_id": str(operation_id)},
            principal=principal,
            idempotency_key=f"cancel-{next(self.keys)}",
        ).answer

    def refused(self, operation_id, code, **asked):
        answer = self.cancel(operation_id, **asked)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["code"], code, answer)
        return answer["error"]

    def state(self, operation_id):
        return OperationRequest.objects.get(pk=operation_id).state

    def buttons(self, page):
        from django.urls import reverse

        return select(page, "formaction", reverse("command", args=[CANCEL]))


class SignedInTestCase(CancelTestCase):
    def setUp(self):
        super().setUp()
        self.person = get_user_model().objects.create_superuser("owner", "owner@example.test", "pw")
        self.client.force_login(self.person)
        # The Run form offers the machines HQ knows.
        machines = patch(
            "hq.platform.application.machines.machine_catalog", side_effect=lambda **_: self.supply.machines
        )
        machines.start()
        self.addCleanup(machines.stop)


class PersonCancelsTests(CancelTestCase):
    def test_a_queued_run_is_cancelled_and_frees_its_machines(self):
        run = self.ask(APP, DB)["operation"]

        answer = self.cancel(run["id"])

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["operation"]["state"], OperationRequest.State.CANCELLED)
        self.assertEqual(self.state(run["id"]), OperationRequest.State.CANCELLED)
        # The machine and the playbook take a run again.
        self.assertTrue(self.ask(DB, name=LOGS_RUN)["queued"])
        self.assertTrue(self.ask(EDGE)["queued"])

    def test_the_cancel_is_audited_as_its_command_by_whoever_cancelled(self):
        run = self.ask(APP)["operation"]

        self.cancel(run["id"])

        row = AuditLog.objects.filter(object_type="Infrastructure operation", metadata__operation=CANCEL).get()
        self.assertEqual(row.metadata["actor"], OPERATOR.actor)

    def test_a_claimed_run_is_left_to_its_runner(self):
        run = self.ask(APP)["operation"]
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))

        error = self.refused(run["id"], "precondition")

        self.assertEqual(error["details"]["operation"]["id"], run["id"])
        self.assertIn("stops reporting", error["message"])
        self.assertEqual(self.state(run["id"]), OperationRequest.State.CLAIMED)

    def test_an_ended_run_is_refused(self):
        run = self.ask(APP)["operation"]
        self.cancel(run["id"])

        self.refused(run["id"], "precondition")

    def test_a_person_without_the_run_s_authority_is_refused(self):
        run = self.ask(APP)["operation"]

        self.refused(run["id"], "forbidden", principal=VIEWER)
        self.assertEqual(self.state(run["id"]), OperationRequest.State.QUEUED)

    def test_an_id_no_run_has_is_not_found(self):
        for unknown in ("00000000-0000-0000-0000-000000000000", "not-an-id"):
            with self.subTest(unknown):
                self.refused(unknown, "not_found")


class AgentCancelsTests(CancelTestCase):
    def test_an_agent_cancels_a_run_it_asked_for(self):
        run = approved_run(self, run_payload(APP), principal=AGENT, key="example-held")["operation"]

        answer = self.cancel(run["id"], principal=AGENT)

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.state(run["id"]), OperationRequest.State.CANCELLED)

    def test_an_agent_s_cancel_waits_for_nobody(self):
        run = approved_run(self, run_payload(APP), principal=AGENT, key="example-held")["operation"]

        self.cancel(run["id"], principal=AGENT)

        self.assertFalse(ApprovalRequest.objects.filter(capability=CANCEL).exists())

    def test_another_s_run_is_no_run_to_an_agent(self):
        mine = approved_run(self, run_payload(APP), principal=AGENT, key="example-held")["operation"]
        persons = self.ask(EDGE, name=LOGS_RUN)["operation"]

        for run in (mine, persons):
            with self.subTest(run["id"]):
                self.refused(run["id"], "not_found", principal=OTHER_AGENT)
                self.assertEqual(self.state(run["id"]), OperationRequest.State.QUEUED)


class CancelButtonTests(SignedInTestCase):
    """A run nothing has claimed carries its Cancel button where it is
    listed, and the button posts the run's id to the cancel command."""

    def test_a_queued_run_s_row_on_jobs_cancels_it(self):
        from django.urls import reverse

        run = self.ask(APP, principal=web_principal(self.person))["operation"]

        (button,) = self.buttons(self.client.get(reverse("jobs:list")))
        self.assertEqual((button.attrs["name"], button.attrs["value"]), ("operation_id", run["id"]))
        pressed = self.client.post(
            button.attrs["formaction"], {"operation_id": run["id"], "__retry_key": "web:example-cancel-press"}
        )

        self.assertEqual(pressed.status_code, 302, text(pressed))
        self.assertEqual(self.state(run["id"]), OperationRequest.State.CANCELLED)
        self.assertEqual(self.buttons(self.client.get(reverse("jobs:list"))), [])

    def test_a_claimed_run_has_no_button(self):
        from django.urls import reverse

        self.ask(APP)
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))

        self.assertEqual(self.buttons(self.client.get(reverse("jobs:list"))), [])

    def test_a_reader_without_the_run_s_authority_has_no_button(self):
        from ..automation_runs import listed_with_actions
        from ..work_runs import runs_on

        self.ask(APP)

        (listed,) = listed_with_actions(runs_on(APP), VIEWER)
        self.assertEqual(listed.actions, ())
        (offered,) = listed_with_actions(runs_on(APP), OPERATOR)
        self.assertEqual(offered.actions[0].value, str(OperationRequest.objects.get().pk))

    def test_a_machine_s_recent_runs_carry_the_button(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        from ..service_context import recent_work
        from ..work_runs import runs_on

        run = self.ask(APP)["operation"]
        section = recent_work(runs_on(APP), by_machine=True, more_url="/jobs/", principal=OPERATOR)
        request = RequestFactory().get("/")
        request.user = self.person

        drawn = render_to_string(
            "partials/_page_sections.html", {"sections": (section,), "spans_row": section.spans_row}, request=request
        )

        (button,) = self.buttons(drawn)
        self.assertEqual(button.attrs["value"], run["id"])

    def test_the_run_page_s_refusal_offers_to_cancel_the_run_in_the_way(self):
        from django.urls import reverse

        holder = self.ask(APP, DB)["operation"]

        page = self.client.post(
            reverse("command", args=[LOGS_RUN]),
            {"hosts": [DB], "parameters.service": "web", "__confirm_effect": "on", "__retry_key": "web:example-2"},
        )

        self.assertEqual(page.status_code, 409)
        (button,) = self.buttons(page)
        self.assertEqual(button.attrs["value"], holder["id"])


class HeldRunApprovedWhileHeldTests(SignedInTestCase):
    """A held run approved while another run holds its machine is refused
    naming that run, offers to cancel it, and stays decidable."""

    def test_the_approver_sees_the_run_in_the_way_and_can_still_approve_once_it_is_gone(self):
        from django.urls import reverse

        held = run_capability(RESTART_RUN, run_payload(DB), principal=AGENT, idempotency_key="example-held").answer[
            "approval"
        ]
        holder = self.ask(DB, name=LOGS_RUN)["operation"]
        decide = reverse("control_plane:approval_decision", kwargs={"approval_id": held["id"]})

        refused = self.client.post(decide, {"decision": "approve", "__retry_key": "web:example-approve-1"}, follow=True)

        said = text(refused)
        self.assertIn(f"{DB} is busy with a run of", said)
        (button,) = self.buttons(refused)
        self.assertEqual(button.attrs["value"], holder["id"])
        self.assertEqual(ApprovalRequest.objects.get(pk=held["id"]).state, ApprovalRequest.State.PENDING)

        self.cancel(holder["id"])
        self.client.post(decide, {"decision": "approve", "__retry_key": "web:example-approve-2"})

        self.assertEqual(ApprovalRequest.objects.get(pk=held["id"]).state, ApprovalRequest.State.APPROVED)
