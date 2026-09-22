"""The agents HQ knows about: learned from tokens, and loud when they change."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import AgentIdentity, AuditLog

from .agent_registry import AUDIT_LABEL, observe
from .security import Capability, Principal


def agent(*granted, interface="mcp", actor="example-agent"):
    return Principal(actor, interface, frozenset(granted), granted=frozenset(granted))


def _events():
    return list(
        AuditLog.objects.filter(object_type=AUDIT_LABEL).order_by("id").values_list("message", flat=True)
    )


class LearningTests(TestCase):
    def test_a_new_identity_is_registered_and_announced(self):
        observe(agent("read", "write_projects"))

        row = AgentIdentity.objects.get()
        self.assertEqual(row.client_id, "example-agent")
        self.assertEqual(row.granted, ["read", "write_projects"])
        self.assertEqual(row.interfaces, ["mcp"])
        self.assertEqual(_events(), ["First seen: example-agent, over mcp, granted 2 permissions"])

    def test_a_widened_grant_is_announced_permission_by_permission(self):
        observe(agent("read"))
        observe(agent("read", "delete_projects"))

        self.assertEqual(AgentIdentity.objects.get().granted, ["delete_projects", "read"])
        self.assertEqual(
            _events()[-1], "Grant for example-agent changed: added delete_projects"
        )

    def test_a_narrowed_grant_is_announced_too(self):
        observe(agent("read", "write_projects"))
        observe(agent("read"))

        self.assertEqual(
            _events()[-1], "Grant for example-agent changed: removed write_projects"
        )

    def test_one_client_on_two_surfaces_is_one_identity(self):
        observe(agent("read", interface="mcp"))
        observe(agent("read", interface="api"))

        self.assertEqual(AgentIdentity.objects.get().interfaces, ["api", "mcp"])


class CostTests(TestCase):
    def test_routine_traffic_does_not_write(self):
        observe(agent("read"))
        with self.assertNumQueries(1):
            observe(agent("read"))

    def test_last_seen_moves_once_the_interval_passes(self):
        observe(agent("read"))
        stale = timezone.now() - timedelta(hours=1)
        AgentIdentity.objects.update(last_seen=stale)

        observe(agent("read"))

        self.assertGreater(AgentIdentity.objects.get().last_seen, stale)


class ScopeTests(TestCase):
    def test_principals_without_a_grant_are_not_identities(self):
        observe(None)
        observe(Principal("op", "web", frozenset({Capability.READ})))
        observe(Principal("mcp-service-account", "mcp", frozenset({Capability.READ})))

        self.assertFalse(AgentIdentity.objects.exists())

    def test_a_failure_to_observe_is_swallowed(self):
        with mock.patch.object(AgentIdentity.objects, "filter", side_effect=RuntimeError("locked")):
            observe(agent("read"))
