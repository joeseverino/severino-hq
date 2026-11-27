"""The agents HQ knows about: learned from tokens, and loud when they change."""

from unittest import mock

from django.test import TestCase

from hq.platform.core.models import AgentIdentity

from ..agent_registry import observe
from ..security import Capability, Principal


def agent(*granted, interface="mcp", actor="example-agent"):
    return Principal(actor, interface, frozenset(granted), granted=frozenset(granted))


class ScopeTests(TestCase):
    def test_principals_without_a_grant_are_not_identities(self):
        observe(None)
        observe(Principal("op", "web", frozenset({Capability.READ})))
        observe(Principal("mcp-service-account", "mcp", frozenset({Capability.READ})))

        self.assertFalse(AgentIdentity.objects.exists())

    def test_a_failure_to_observe_is_swallowed(self):
        with mock.patch.object(AgentIdentity.objects, "filter", side_effect=RuntimeError("locked")):
            observe(agent("read"))
