"""The agents page's save through the policy capability."""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application.capability_policy import SET_POLICY, Rule, field_name
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.security import OPERATOR_CAPABILITIES, Capability, Principal
from hq.platform.core.models import AgentIdentity, AuditLog
from hq.platform.core.testing import operator


class PolicyWriteTests(TestCase):
    """The page's save runs ``agent.policy.set``, which only a person holds."""

    def setUp(self):
        operator(self.client, "op")
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "delete_projects"])
        self.field = field_name("example-agent", "project.delete")

    def save(self, rule):
        return self.client.post(reverse("agent_policy"), form_keyed({self.field: rule}), follow=True)

    def denials(self) -> int:
        return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_POLICY).count()

    def test_an_operator_without_the_grant_is_refused_on_the_page(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.GOVERN_AGENTS})
        with patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            self.save(Rule.APPROVE)

        self.assertFalse(CapabilityRule.objects.exists())
        self.assertEqual(self.denials(), 1)


class PolicyRefusalTests(TestCase):
    """A save that is refused, whole or in part, is said over the rules' tables, on the page it was sent from."""

    def setUp(self):
        operator(self.client, "op")
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "delete_projects"])

    def save(self, rules):
        return self.client.post(reverse("agent_policy"), form_keyed(rules))

    def test_a_rule_that_could_not_change_is_said_beside_the_ones_that_did(self):
        response = self.save(
            {
                field_name("example-agent", "project.delete"): Rule.DENY,
                field_name("example-agent", "example.retired"): Rule.DENY,
            }
        )

        self.assertEqual(response.status_code, 400)
        (refused,) = response.context["form"].non_field_errors()
        self.assertIn("example.retired", refused)
        self.assertEqual(CapabilityRule.objects.get().rule, Rule.DENY)
