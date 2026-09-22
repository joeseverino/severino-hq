"""Refusals, recorded: attributed when the caller is known, counted when not."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog

from .capabilities import execute_capability
from .denials import LABEL, record_denial
from .security import Capability, Principal

A_TOKEN_VALUE = "not.a.real-token-but-must-never-be-kept"


def _denials():
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED).order_by("id")


class AuthenticatedRefusalTests(TestCase):
    def test_a_capability_an_agent_lacks_is_recorded_against_that_agent(self):
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        result = execute_capability("project.create", {"name": "Nope"}, principal=agent)

        self.assertFalse(result["ok"])
        row = _denials().get()
        self.assertEqual(row.metadata["actor"], "example-agent")
        self.assertEqual(row.metadata["interface"], "mcp")
        self.assertEqual(row.metadata["capability"], "project.create")
        self.assertTrue(row.metadata["authenticated"])
        self.assertEqual(row.actor_label, "example-agent")

    def test_every_authenticated_refusal_gets_its_own_row(self):
        for _ in range(3):
            record_denial(interface="mcp", reason="agents_paused", actor="example-agent")

        self.assertEqual(_denials().count(), 3)


class UnauthenticatedRefusalTests(TestCase):
    def test_repeats_from_one_source_are_counted_not_multiplied(self):
        for _ in range(5):
            record_denial(
                interface="mcp", reason="invalid_credential", source="100.64.0.7", authenticated=False
            )

        row = _denials().get()
        self.assertEqual(row.metadata["count"], 5)
        self.assertEqual(row.actor_label, "unauthenticated · 100.64.0.7")

    def test_a_different_source_is_a_different_row(self):
        for source in ("100.64.0.7", "100.64.0.8"):
            record_denial(interface="mcp", reason="invalid_credential", source=source, authenticated=False)

        self.assertEqual(_denials().count(), 2)

    def test_the_window_closes(self):
        record_denial(interface="api", reason="invalid_token", source="100.64.0.7", authenticated=False)
        _denials().update(created_at=timezone.now() - timedelta(minutes=5))

        record_denial(interface="api", reason="invalid_token", source="100.64.0.7", authenticated=False)

        self.assertEqual(_denials().count(), 2)


class NeverInTheWayTests(TestCase):
    def test_a_failure_to_record_is_swallowed(self):
        with mock.patch("application.denials.record_event", side_effect=RuntimeError("disk full")):
            record_denial(interface="mcp", reason="agents_paused", actor="example-agent")

        self.assertEqual(_denials().count(), 0)


@override_settings(SEVERINO_API_RESOURCE="https://hq.example.test/api")
class ApiDoorTests(TestCase):
    def test_a_rejected_token_is_counted_and_its_value_is_never_kept(self):
        for _ in range(2):
            response = self.client.get(
                "/api/v2/", HTTP_AUTHORIZATION=f"Bearer {A_TOKEN_VALUE}"
            )
            self.assertEqual(response.status_code, 401)

        row = _denials().get()
        self.assertEqual(row.metadata["interface"], "api")
        self.assertFalse(row.metadata["authenticated"])
        self.assertEqual(row.metadata["count"], 2)
        self.assertNotIn(A_TOKEN_VALUE, str(row.metadata) + row.message)

    def test_a_missing_token_is_recorded(self):
        self.client.get("/api/v2/")

        self.assertEqual(_denials().get().metadata["reason"], "invalid_token")


class WhoColumnTests(TestCase):
    def test_a_machine_actor_is_named_in_the_list_not_called_system(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        record_denial(interface="mcp", reason="agents_paused", actor="example-agent")

        page = self.client.get(reverse("core:audit_list")).content.decode()

        self.assertIn("example-agent", page)

    def test_the_label_prefers_a_person_then_an_agent_then_an_address(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        cases = (
            (AuditLog(user=user, metadata={"actor": "example-agent"}), "op"),
            (AuditLog(metadata={"actor": "example-agent"}), "example-agent"),
            (AuditLog(metadata={"source": "100.64.0.7"}), "unauthenticated · 100.64.0.7"),
            (AuditLog(metadata={}), "system"),
        )
        for row, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(row.actor_label, expected)


class LabelTests(TestCase):
    def test_denials_are_filed_under_one_label(self):
        record_denial(interface="mcp", reason="agents_paused", actor="example-agent")

        self.assertEqual(_denials().get().object_type, LABEL)
