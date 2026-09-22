"""Tests for the agent pause switch."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse

from core.models import AgentAccess, AuditLog

from .agent_access import agents_paused, set_agents_paused
from .security import AuthorizationError, Capability, Principal


def _said():
    return [
        event.message
        for event in AuditLog.objects.filter(object_type="Agent access").order_by("id")
    ]


class StateTests(TestCase):
    def test_a_fresh_deployment_allows_agents(self):
        self.assertFalse(AgentAccess.objects.exists())
        self.assertFalse(agents_paused())

    def test_an_unreadable_switch_refuses_agents(self):
        with mock.patch.object(
            AgentAccess.objects, "filter", side_effect=DatabaseError("locked")
        ):
            self.assertTrue(agents_paused())


class WhoMayPullItTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)

    def test_no_credential_can_change_it(self):
        for interface in ("mcp", "api", "cli"):
            credential = Principal("example-agent", interface, frozenset({Capability.READ}))
            with self.subTest(interface=interface), self.assertRaises(AuthorizationError):
                set_agents_paused(True, principal=credential, user=self.user)

        self.assertFalse(agents_paused())
        self.assertEqual(_said(), [])

    def test_the_change_does_not_happen_without_its_record(self):
        person = Principal("op", "web", frozenset())
        with mock.patch(
            "application.agent_access.record_event", side_effect=RuntimeError("audit down")
        ), self.assertRaises(RuntimeError):
            set_agents_paused(True, principal=person, user=self.user)

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

    def test_an_unrecognised_state_changes_nothing(self):
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
