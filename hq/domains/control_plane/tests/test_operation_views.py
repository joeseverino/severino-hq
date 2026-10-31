"""A verb pressed on a record's page is its capability, run as the operator."""

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.approvals import DESTRUCTIVE_EFFECT, ApproveCommand, approve, asked, preview
from hq.platform.application.capabilities import capability_registry, execute_capability
from hq.platform.application.capability_policy import Rule, Scope
from hq.platform.application.resource_operations import ACTION_CAPABILITY
from hq.platform.application.security import Capability, Principal, web_principal
from hq.platform.core.models import AuditLog

CONTAINER = {"connection_ref": "example-portainer", "host": "example-host", "name": "example-web"}
LIFECYCLE = (OperationRequest.Action.RESTART, OperationRequest.Action.START, OperationRequest.Action.STOP)
STOP = ACTION_CAPABILITY[OperationRequest.Action.STOP]
MANAGES = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})


def agent(actor="example-agent", interface="mcp"):
    return Principal(actor, interface, MANAGES, granted=frozenset(str(c) for c in MANAGES))


class LifecycleVerbTests(TestCase):
    def setUp(self):
        managing_everything()
        self.container = ManagedResource.objects.create(key="example-web", kind=CONTAINER_KIND, spec=CONTAINER)
        self.user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        self.client.force_login(self.user)

    def press(self, action, **data):
        return self.client.post(reverse(f"control_plane:{action}", kwargs={"key": "example-web"}), data)

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

    def test_a_token_without_the_grant_is_refused_before_anything_is_queued(self):
        reader = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        for action in LIFECYCLE:
            with self.subTest(action=action):
                result = execute_capability(ACTION_CAPABILITY[action], {}, principal=reader, target="example-web")

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(OperationRequest.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.DENIED).count(), len(LIFECYCLE))

    def test_input_the_command_does_not_take_is_refused(self):
        result = execute_capability(
            ACTION_CAPABILITY[OperationRequest.Action.RESTART],
            {"reason": "Example.", "action": "delete"},
            principal=Principal("operator", "web", frozenset({Capability.MANAGE_INFRASTRUCTURE})),
            target="example-web",
        )

        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(OperationRequest.objects.exists())


class RemovalVerbTests(TestCase):
    def setUp(self):
        managing_everything()
        ManagedResource.objects.create(
            key="example-dns", kind="adguard.rewrite", spec={"domain": "app.example.com", "answer": "192.0.2.10"}
        )
        self.client.force_login(get_user_model().objects.create_user(username="operator", password="x" * 20))

    def test_removal_is_queued_and_audited_under_the_operator(self):
        response = self.client.post(reverse("control_plane:remove", kwargs={"key": "example-dns"}), {"reason": "Gone."})

        self.assertRedirects(response, reverse("control_plane:detail", kwargs={"key": "example-dns"}))
        operation = OperationRequest.objects.get(action=OperationRequest.Action.DELETE)
        self.assertEqual((operation.requested_actor, operation.reason), ("operator", "Gone."))
        self.assertTrue(
            AuditLog.objects.filter(object_id=str(operation.pk), metadata__actor="operator").exists(),
        )


class AgentStopTests(TestCase):
    """A stop takes a container away from whoever uses it, so a credential's
    waits for a person unless a rule allows it; a person in the browser's runs."""

    def setUp(self):
        managing_everything()
        ManagedResource.objects.create(key="example-web", kind=CONTAINER_KIND, spec=CONTAINER)
        self.operator = web_principal(get_user_model().objects.create_user(username="operator", password="x" * 20))

    def stop(self, principal):
        return execute_capability(STOP, {"reason": "Example."}, principal=principal, target="example-web")

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
        self.assertEqual(str(asked(waiting)), "example-agent wants to stop example-web")

        approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)

        operation = OperationRequest.objects.get(action=OperationRequest.Action.STOP)
        self.assertEqual(operation.requested_actor, "example-agent")

    def test_an_allow_for_the_agent_or_its_surface_lets_the_stop_run(self):
        for scope, subject in ((Scope.AGENT, "example-agent"), (Scope.SURFACE, "api")):
            with self.subTest(scope=scope):
                OperationRequest.objects.all().delete()
                CapabilityRule.objects.all().delete()
                CapabilityRule.objects.create(scope=scope, subject=subject, capability=STOP, rule=Rule.ALLOW)

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
        ManagedResource.objects.create(key="example-web", kind=CONTAINER_KIND, spec=CONTAINER)
        self.client.force_login(get_user_model().objects.create_user(username="operator", password="x" * 20))

    def test_a_verb_is_confirmed_on_a_page_of_its_own_exactly_when_its_capability_is_destructive(self):
        registry = capability_registry()
        pages = {action: _verb_url(action) for action in ACTION_CAPABILITY}
        pages = {action: url for action, url in pages.items() if url}
        confirmed = set()
        for action, url in pages.items():
            response = self.client.get(url)
            with self.subTest(action=action):
                self.assertIn(response.status_code, (200, 405))
            if response.status_code == 200:
                confirmed.add(action)
        destructive = {action for action in pages if registry[ACTION_CAPABILITY[action]].effect == DESTRUCTIVE_EFFECT}

        self.assertIn(OperationRequest.Action.STOP, confirmed)
        self.assertEqual(confirmed, destructive)


def _verb_url(action) -> str:
    """The page a resource's verb posts to, or "" for one with a view of its own (removal)."""

    try:
        return reverse(f"control_plane:{action}", kwargs={"key": "example-web"})
    except NoReverseMatch:
        return ""
