"""A verb pressed on a record's page is its capability, run as the operator."""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, OperationRequest
from hq.domains.control_plane.observations.portainer import CONTAINER_KIND
from hq.platform.application.approval_review import asked, preview
from hq.platform.application.approvals import ApproveCommand, approve
from hq.platform.application.capabilities import command_spec, execute_capability
from hq.platform.application.capability_policy import Rule
from hq.platform.application.contracts import Effect
from hq.platform.application.markup_testing import select
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.resource_operations import ACTION_CAPABILITY
from hq.platform.application.security import Capability, Principal, web_principal
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import declare, managing_everything, operator

CONTAINER = {"connection_ref": "example-portainer", "host": "example-host", "name": "example-web"}
LIFECYCLE = (OperationRequest.Action.RESTART, OperationRequest.Action.START, OperationRequest.Action.STOP)
STOP = ACTION_CAPABILITY[OperationRequest.Action.STOP]
MANAGES = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})


def agent(actor="example-agent", interface="mcp"):
    return Principal(actor, interface, MANAGES, granted=frozenset(str(c) for c in MANAGES))


class LifecycleVerbTests(TestCase):
    def setUp(self):
        managing_everything()
        self.container = declare("example-web", CONTAINER_KIND, CONTAINER)
        self.user = operator(self.client)

    def press(self, action, **data):
        return self.client.post(reverse(f"control_plane:{action}", kwargs={"key": "example-web"}), form_keyed(data))

    def test_each_verb_queues_its_operation_under_the_operator(self):
        for action in LIFECYCLE:
            with self.subTest(action=action):
                response = self.press(action, reason="Example reason.")

                self.assertEqual(response.status_code, 302)
                operation = OperationRequest.objects.get(action=action)
                self.assertEqual((operation.requested_actor, operation.requested_interface), ("operator", "web"))
                self.assertEqual(operation.reason, "Example reason.")
                row = AuditLog.objects.filter(object_type="Infrastructure operation", object_id=str(operation.pk))
                self.assertEqual(row.get().metadata["actor"], "operator")

    def test_a_refusal_says_why_and_queues_nothing(self):
        self.container.enabled = False
        self.container.save(update_fields=["enabled"])

        response = self.press(OperationRequest.Action.RESTART)

        said = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(said, ["This record is switched off in HQ."])
        self.assertFalse(OperationRequest.objects.exists())


class RemovalVerbTests(TestCase):
    def setUp(self):
        managing_everything()
        declare("example-dns", "adguard.rewrite", {"domain": "app.example.com", "answer": "192.0.2.10"})
        operator(self.client)

    def test_removal_is_queued_and_audited_under_the_operator(self):
        response = self.client.post(
            reverse("control_plane:remove", kwargs={"key": "example-dns"}), form_keyed({"reason": "Gone."})
        )

        self.assertRedirects(response, reverse("control_plane:detail", kwargs={"key": "example-dns"}))
        operation = OperationRequest.objects.get(action=OperationRequest.Action.DELETE)
        self.assertEqual((operation.requested_actor, operation.reason), ("operator", "Gone."))
        self.assertTrue(
            AuditLog.objects.filter(object_id=str(operation.pk), metadata__actor="operator").exists(),
        )

    def test_the_reason_is_a_field_posted_with_the_confirmation_and_described_by_its_help(self):
        page = self.client.get(reverse("control_plane:remove", kwargs={"key": "example-dns"}))

        (reason,) = select(page, "name", "reason")
        self.assertEqual(reason.attrs["form"], "hq-post")
        (label,) = select(page, "for", reason.attrs["id"])
        self.assertIn("Why", label.text)
        (help_,) = select(page, "id", reason.attrs["aria-describedby"])
        self.assertTrue(help_.text.strip())


class AgentStopTests(TestCase):
    """A stop takes a container away from whoever uses it, so a credential's
    waits for a person unless a rule allows it; a person in the browser's runs."""

    def setUp(self):
        managing_everything()
        declare("example-web", CONTAINER_KIND, CONTAINER)
        self.operator = web_principal(operator())

    def stop(self, principal):
        return execute_capability(STOP, keyed({"reason": "Example."}), principal=principal, target="example-web")

    def test_an_agents_stop_waits_for_a_person_on_either_surface(self):
        for interface in ("mcp", "api"):
            with self.subTest(interface=interface):
                result = self.stop(agent(interface=interface))

                self.assertEqual(result.get("status"), "awaiting_approval", result)
                self.assertFalse(OperationRequest.objects.exists())
        self.assertEqual(ApprovalRequest.objects.filter(capability=STOP).count(), 2)

    def test_a_held_stop_is_queued_once_approved(self):
        held = self.stop(agent())
        waiting = ApprovalRequest.objects.get()
        shown = preview(waiting)
        # The act is the whole of it, said by its sentence.
        self.assertEqual((shown.rows, shown.lines), ((), ()))
        self.assertEqual(str(asked(waiting)), "example-agent wants to stop the container example-web")

        approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)

        operation = OperationRequest.objects.get(action=OperationRequest.Action.STOP)
        self.assertEqual(operation.requested_actor, "example-agent")

    def test_on_for_the_agent_lets_its_stop_run(self):
        CapabilityRule.objects.create(subject="example-agent", capability=STOP, rule=Rule.ALLOW)

        result = self.stop(agent(interface="api"))

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(OperationRequest.objects.filter(action=OperationRequest.Action.STOP).exists())
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_person_in_the_browser_is_never_held(self):
        result = self.stop(self.operator)

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(OperationRequest.objects.filter(action=OperationRequest.Action.STOP).exists())
        self.assertFalse(ApprovalRequest.objects.exists())


class ConfirmedVerbTests(TestCase):
    """The web asks a person to confirm exactly the verbs whose capability is
    destructive, so the page and the agents' default read one declaration."""

    def setUp(self):
        managing_everything()
        declare("example-web", CONTAINER_KIND, CONTAINER)
        operator(self.client)

    def test_a_verb_is_confirmed_on_a_page_of_its_own_exactly_when_its_capability_is_destructive(self):
        pages = {action: _verb_url(action) for action in ACTION_CAPABILITY}
        pages = {action: url for action, url in pages.items() if url}
        confirmed = set()
        for action, url in pages.items():
            response = self.client.get(url)
            with self.subTest(action=action):
                self.assertIn(response.status_code, (200, 405))
            if response.status_code == 200:
                confirmed.add(action)
        destructive = {
            action for action in pages if command_spec(ACTION_CAPABILITY[action]).effect is Effect.DESTRUCTIVE
        }

        self.assertIn(OperationRequest.Action.STOP, confirmed)
        self.assertEqual(confirmed, destructive)


def _verb_url(action) -> str:
    """The page a resource's verb posts to, or "" for one with a view of its own (removal)."""

    try:
        return reverse(f"control_plane:{action}", kwargs={"key": "example-web"})
    except NoReverseMatch:
        return ""
