"""The agents page's save through the policy capability."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.capability_policy import (
    SET_POLICY,
    Rule,
    field_name,
    submitted_rules,
)
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal, web_principal
from hq.platform.application.tests.markup import text
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AgentIdentity, AuditLog


class PolicyWriteTests(TestCase):
    """The page's save runs ``agent.policy.set``, which only a person holds."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "delete_projects"])
        self.field = field_name("example-agent", "project.delete")

    def save(self, rule):
        return self.client.post(reverse("agent_policy"), form_keyed({self.field: rule}), follow=True)

    def denials(self) -> int:
        return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_POLICY).count()

    def test_a_saved_rule_is_recorded_under_the_operator(self):
        response = self.save(Rule.APPROVE)

        self.assertIn("Saved 1 change", text(response))
        saved = AuditLog.objects.get(object_type="Capability policy")
        self.assertEqual((saved.user, saved.metadata["operation"]), (self.user, SET_POLICY))
        self.assertEqual(CapabilityRule.objects.get().rule, Rule.APPROVE)

    def test_an_operator_without_the_grant_is_refused_on_the_page(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.GOVERN_AGENTS})
        with patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            response = self.save(Rule.APPROVE)

        self.assertIn("govern_agents", text(response))
        self.assertFalse(CapabilityRule.objects.exists())
        self.assertEqual(self.denials(), 1)

    def test_no_credential_sets_policy(self):
        granted = frozenset(Capability)
        cell = {"subject": "example-agent", "capability": "project.delete", "rule": Rule.ALLOW}
        for principal in (
            Principal("example-agent", "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            with self.subTest(interface=principal.interface):
                result = execute_capability(SET_POLICY, keyed({"rules": [cell]}), principal=principal)

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(CapabilityRule.objects.exists())
        self.assertEqual(self.denials(), 3)

    def test_rules_that_are_not_cells_are_invalid(self):
        for rules in ("project.delete", [{"subject": "example-agent"}]):
            with self.subTest(rules=rules):
                result = execute_capability(SET_POLICY, keyed({"rules": rules}), principal=web_principal(self.user))

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(CapabilityRule.objects.exists())

    def test_the_form_names_each_cell_by_its_agent_and_capability(self):
        post = {self.field: Rule.DENY, "csrfmiddlewaretoken": "x", "rule|too-few": "allow", "rule|a|b|c": "allow"}

        self.assertEqual(
            submitted_rules(post),
            [{"subject": "example-agent", "capability": "project.delete", "rule": Rule.DENY}],
        )
