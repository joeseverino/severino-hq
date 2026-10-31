"""Tests for the agent pause switch."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse

from hq.platform.core.models import AgentAccess, AuditLog

from ..agent_access import SET_AGENT_ACCESS, AgentAccessCommand, agents_paused, set_agents_paused
from ..capabilities import execute_capability
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal, web_principal


def _said():
    return [event.message for event in AuditLog.objects.filter(object_type="Agent access").order_by("id")]


def _denials() -> int:
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_AGENT_ACCESS).count()


class StateTests(TestCase):
    def test_a_fresh_deployment_allows_agents(self):
        self.assertFalse(AgentAccess.objects.exists())
        self.assertFalse(agents_paused())

    def test_an_unreadable_switch_refuses_agents(self):
        with mock.patch.object(AgentAccess.objects, "filter", side_effect=DatabaseError("locked")):
            self.assertTrue(agents_paused())


class WhoMayPullItTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)

    def test_no_credential_can_change_it(self):
        granted = frozenset(Capability)
        for credential in (
            Principal("example-agent", "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            with self.subTest(interface=credential.interface):
                result = execute_capability(SET_AGENT_ACCESS, {"paused": True}, principal=credential)

                self.assertEqual(result["error"]["code"], "forbidden")

        self.assertFalse(agents_paused())
        self.assertEqual(_said(), [])
        self.assertEqual(_denials(), 3)

    def test_a_state_that_is_not_a_yes_or_no_is_invalid(self):
        for payload in ({}, {"paused": "sometimes"}):
            with self.subTest(payload=payload):
                result = execute_capability(SET_AGENT_ACCESS, payload, principal=web_principal(self.user))

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(agents_paused())

    def test_the_change_does_not_happen_without_its_record(self):
        with (
            mock.patch("hq.platform.application.agent_access.record_event", side_effect=RuntimeError("audit down")),
            self.assertRaises(RuntimeError),
        ):
            set_agents_paused(AgentAccessCommand(paused=True), principal=web_principal(self.user))

        self.assertFalse(agents_paused())


class SwitchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)

    def _set(self, paused: str):
        return self.client.post(reverse("agent_access"), {"paused": paused})

    def test_pausing_holds_and_is_attributed(self):
        self._set("1")

        self.assertTrue(agents_paused())
        event = AuditLog.objects.get(object_type="Agent access")
        self.assertEqual(event.message, "Agents paused")
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.metadata["operation"], SET_AGENT_ACCESS)

    def test_an_operator_without_the_grant_is_refused_with_the_reason(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.GOVERN_AGENTS})
        with mock.patch("hq.platform.core.views.web_principal", return_value=lacking):
            refused = self._set("1")

        self.assertEqual(refused.status_code, 403)
        self.assertIn(b"govern_agents", refused.content)
        self.assertFalse(agents_paused())
        self.assertEqual(_denials(), 1)

    def test_a_double_click_on_pause_stays_paused(self):
        self._set("1")
        self._set("1")

        self.assertTrue(agents_paused())
        self.assertEqual(_said(), ["Agents paused"])

    def test_both_directions_are_recorded(self):
        self._set("1")
        self._set("0")

        self.assertFalse(agents_paused())
        self.assertEqual(_said(), ["Agents paused", "Agents resumed"])

    def test_a_get_cannot_change_it(self):
        response = self.client.get(reverse("agent_access"), {"paused": "1"})

        self.assertEqual(response.status_code, 405)
        self.assertFalse(agents_paused())

    def test_an_unrecognized_state_changes_nothing(self):
        response = self._set("yes")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(agents_paused())

    def test_signed_out_it_cannot_be_reached(self):
        self.client.logout()

        self._set("1")

        self.assertFalse(agents_paused())

    def test_the_menu_shows_the_state_and_asks_for_the_other_one(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('name="paused" value="1"', page)

        self._set("1")

        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('name="paused" value="0"', page)
