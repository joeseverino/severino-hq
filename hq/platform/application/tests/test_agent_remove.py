"""A person's removal of one agent: its identity, devices and own rules go, its history stays, and it grants nothing."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import CapabilityRule
from hq.domains.projects.models import Project
from hq.platform.api.tests.tokens import ISSUER
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentDevice, AgentIdentity, AuditLog

from ..capabilities import capability_registry, execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal, subject_of, web_principal
from .test_agent_devices import (
    API,
    AWAITING,
    CLIENT,
    LAPTOP,
    LAPTOP_AT,
    LAPTOP_ID,
    PERSON,
    Doors,
    _token_for,
    a_tailnet,
)

# Written out, so the module imports at a base that has no removal.
REMOVE = "agent.remove"
BLOCK = "agent.block.set"
AGENT = "example-agent"
OTHER = "example-other-agent"


def _seen(*clients: str, person_key: str = "") -> AgentIdentity:
    for client in clients:
        row = AgentIdentity.objects.create(
            client_id=client, person_key=person_key, interfaces=["mcp"], granted=["read"]
        )
    return row


def _rule(subject: str, capability: str = "project.create") -> CapabilityRule:
    return CapabilityRule.objects.create(subject=subject, capability=capability, rule=CapabilityRule.Rule.DENY)


class RemovalTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        self.key = "example-key"
        bind_sign_in(self.person, self.key)
        self.row = _seen(AGENT, person_key=self.key)
        self.subject = subject_of(AGENT, self.key)
        AgentDevice.objects.create(identity=self.row, person=self.person, node_id="nEXAMPLE1", name="example-laptop")
        _rule(self.subject)
        _seen(OTHER)
        _rule(OTHER)

    def _remove(self, target=None, payload=None, principal=None):
        return execute_capability(
            REMOVE,
            payload if payload is not None else keyed(),
            principal=principal or web_principal(self.person),
            target=target or self.subject,
        )

    def test_the_agent_its_devices_and_its_own_rules_go_and_one_event_names_who_removed_it(self):
        answer = self._remove()

        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["client_id"], answer["devices"], answer["rules"]), (AGENT, 1, 1))
        self.assertFalse(AgentIdentity.objects.filter(client_id=AGENT).exists())
        self.assertFalse(AgentDevice.objects.exists())
        self.assertFalse(CapabilityRule.objects.filter(subject=self.subject).exists())
        self.assertTrue(AgentIdentity.objects.filter(client_id=OTHER).exists(), "the agent beside it stays")
        self.assertTrue(CapabilityRule.objects.filter(subject=OTHER).exists(), "another agent's rule stays")
        (event,) = AuditLog.objects.filter(metadata__operation=REMOVE)
        self.assertEqual(event.user, self.person)
        self.assertEqual(event.action, AuditLog.Action.DELETED)
        self.assertEqual((event.metadata["client_id"], event.metadata["subject"]), (AGENT, self.subject))

    def test_the_audit_log_is_kept(self):
        before = set(AuditLog.objects.values_list("pk", flat=True))

        self._remove()

        self.assertLessEqual(before, set(AuditLog.objects.values_list("pk", flat=True)))

    def test_a_retry_with_the_same_key_answers_the_first_result_and_records_once(self):
        payload = keyed()
        first = self._remove(payload=payload)
        again = self._remove(payload=payload)

        self.assertEqual(again, first)
        self.assertEqual(AuditLog.objects.filter(metadata__operation=REMOVE).count(), 1)

    def test_a_client_hq_has_never_seen_is_refused_as_the_block_refuses_it(self):
        removed = self._remove(target="example-unseen")
        blocked = execute_capability(
            BLOCK, keyed({"blocked": True}), principal=web_principal(self.person), target="example-unseen"
        )

        self.assertEqual(removed["error"]["code"], blocked["error"]["code"])
        self.assertEqual(removed["error"]["code"], "not_found")

    def test_a_blocked_agent_is_refused_and_stays_with_its_devices(self):
        blocked = execute_capability(
            BLOCK, keyed({"blocked": True}), principal=web_principal(self.person), target=self.subject
        )
        self.assertTrue(blocked["ok"], blocked)

        answer = self._remove()

        self.assertEqual(answer["error"]["code"], "precondition")
        self.assertIn("Allow it first", answer["error"]["message"])
        self.assertTrue(AgentIdentity.objects.filter(client_id=AGENT, blocked_at__isnull=False).exists())
        self.assertTrue(AgentDevice.objects.exists())
        self.assertTrue(CapabilityRule.objects.filter(subject=self.subject).exists())


class WhoMayRemoveTests(TestCase):
    def setUp(self):
        _seen(AGENT)

    def test_it_is_held_to_the_block_s_holders(self):
        registry = capability_registry()

        self.assertEqual(registry[REMOVE].required_capabilities, registry[BLOCK].required_capabilities)

    def test_no_credential_the_block_refuses_removes_an_agent(self):
        granted = frozenset(Capability)
        for credential in (
            Principal(AGENT, "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            with self.subTest(interface=credential.interface):
                blocked = execute_capability(BLOCK, keyed({"blocked": True}), principal=credential, target=AGENT)
                removed = execute_capability(REMOVE, keyed(), principal=credential, target=AGENT)

                self.assertEqual(blocked["error"]["code"], "forbidden")
                self.assertEqual(removed["error"]["code"], "forbidden")
        self.assertTrue(AgentIdentity.objects.filter(client_id=AGENT).exists())


class RemoveControlTests(TestCase):
    def setUp(self):
        _seen(AGENT)
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.person)

    def _post(self, data):
        return self.client.post(reverse("agent_block"), form_keyed(data))

    def test_the_agents_page_offers_removal_and_its_press_removes(self):
        page = self.client.get(reverse("agent_policy"))
        self.assertContains(page, 'name="removed"')

        response = self._post({"client_id": AGENT, "removed": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AgentIdentity.objects.filter(client_id=AGENT).exists())

    def test_a_blocked_agent_s_row_offers_no_removal(self):
        AgentIdentity.objects.filter(client_id=AGENT).update(blocked_at="2026-11-01T12:00:00Z")

        page = self.client.get(reverse("agent_policy"))

        self.assertNotContains(page, 'name="removed"')

    def test_an_operator_without_the_grant_is_refused(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.GOVERN_AGENTS})
        with mock.patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            response = self._post({"client_id": AGENT, "removed": "1"})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(AgentIdentity.objects.filter(client_id=AGENT).exists())

    def test_an_unrecognized_request_changes_nothing(self):
        self.assertEqual(self._post({"client_id": AGENT, "removed": "0"}).status_code, 400)
        self.assertEqual(self._post({"client_id": "", "removed": "1"}).status_code, 400)
        self.assertEqual(self._post({"client_id": AGENT, "removed": "1", "blocked": "1"}).status_code, 400)
        self.assertEqual(self._post({"client_id": "example-unseen", "removed": "1"}).status_code, 404)
        self.assertTrue(AgentIdentity.objects.filter(client_id=AGENT, blocked_at__isnull=True).exists())

    def test_a_get_cannot_remove(self):
        response = self.client.get(reverse("agent_block"), {"client_id": AGENT, "removed": "1"})

        self.assertEqual(response.status_code, 405)
        self.assertTrue(AgentIdentity.objects.filter(client_id=AGENT).exists())


@API
class SignInAfterRemovalTests(Doors, TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user(username="operator", password="test-only-password")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))
        self.enterContext(a_tailnet((LAPTOP, LAPTOP_AT, LAPTOP_ID)))

    def test_a_removed_agent_that_signs_in_again_is_a_new_one_whose_device_waits(self):
        token = _token_for()
        self.codes(token, LAPTOP_AT, "first")
        old = AgentIdentity.objects.get(client_id=CLIENT)
        device = AgentDevice.objects.get(identity=old)
        approved = execute_capability(
            "agent.device_approval.set",
            keyed({"approved": True}),
            principal=web_principal(self.person),
            target=str(device.pk),
        )
        self.assertTrue(approved["ok"], approved)
        self.assertEqual(self.codes(token, LAPTOP_AT, "approved"), (None, None))
        subject = subject_of(CLIENT, old.person_key)

        removed = execute_capability(REMOVE, keyed(), principal=web_principal(self.person), target=subject)
        self.assertTrue(removed["ok"], removed)

        self.assertEqual(self.codes(token, LAPTOP_AT, "again"), (AWAITING, AWAITING))
        fresh = AgentIdentity.objects.get(client_id=CLIENT)
        self.assertNotEqual(fresh.pk, old.pk)
        self.assertIsNone(AgentDevice.objects.get(identity=fresh).approved_at)
        self.assertFalse(Project.objects.filter(slug__startswith="again").exists())
