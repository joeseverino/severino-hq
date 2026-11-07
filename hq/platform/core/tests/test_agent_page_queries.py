"""The Agents page and its every-rule part cost the same however many agents
there are, each with its person, its block, its own rules, its events and the
devices it calls from."""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application import denials, roles
from hq.platform.application.capability_policy import Rule
from hq.platform.application.linked_accounts import bind_sign_in
from hq.platform.core.audit import operation_context
from hq.platform.core.models import AgentDevice, AgentIdentity, AuditLog
from hq.platform.core.test_runner import no_default_role

EVERY_PART = {"HTTP_X_FRAGMENT": "every_rule"}


class AgentPageQueryBudgetTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser("owner", "owner@example.test", "pw")
        self.client.force_login(self.owner)
        self.agents = 0

    def add_agents(self, upto: int) -> None:
        """Agents up to ``upto``, each with everything its row and its column draw."""

        while self.agents < upto:
            index = self.agents
            self.agents += 1
            with no_default_role():
                person = get_user_model().objects.create_user(f"person-{index}")
            bind_sign_in(person, f"person-{index}")
            roles.assign(person, roles.Role.VIEWER)
            client = f"example-agent-{index}"
            identity = AgentIdentity.objects.create(
                client_id=client,
                interfaces=["mcp"],
                granted=["read"],
                person=person,
                blocked_at=timezone.now(),
                blocked_by=self.owner,
            )
            CapabilityRule.objects.create(
                subject=client, capability="project.delete", rule=Rule.DENY, changed_by=self.owner
            )
            for _ in range(3):
                denials.record_denial(interface="mcp", reason="forbidden", actor=client)
            # A device approved by one person and blocked by another, one awaiting
            # its person, each with its calls, and an address the reading does not hold.
            for node, state in (
                (
                    "approved",
                    {
                        "approved_at": timezone.now(),
                        "approved_by": person,
                        "blocked_at": timezone.now(),
                        "blocked_by": self.owner,
                    },
                ),
                ("awaiting", {}),
            ):
                node_id = f"n{node}{index}CNTRL"
                AgentDevice.objects.create(identity=identity, person=person, node_id=node_id, name=node_id, **state)
                AuditLog.objects.create(
                    action=AuditLog.Action.UPDATED, metadata={"actor": client, "interface": "mcp", "device": node_id}
                )
            # And a call from an address the tailnet could not place, refused there.
            with operation_context(interface="mcp", actor=client, operation="example"):
                denials.record_denial(interface="mcp", reason="device_unknown", actor=client, source=f"10.0.0.{index}")

    def queries(self, **headers) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("agent_policy"), **headers)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)

    def test_the_page_costs_the_same_with_one_two_or_four_agents(self):
        counts = []
        for agents in (1, 2, 4):
            self.add_agents(agents)
            counts.append(self.queries())

        self.assertEqual(len(set(counts)), 1, counts)

    def test_every_rule_costs_the_same_with_one_two_or_four_agents(self):
        counts = []
        for agents in (1, 2, 4):
            self.add_agents(agents)
            counts.append(self.queries(**EVERY_PART))

        self.assertEqual(len(set(counts)), 1, counts)
