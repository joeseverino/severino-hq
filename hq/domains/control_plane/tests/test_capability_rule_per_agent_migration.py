"""A rule names one agent: every rule that covered a whole surface is gone, and
every agent's rule stays as it was."""

from django.db import IntegrityError, transaction

from hq.platform.core.tests.migration_steps import MigrationStepTestCase

BEFORE = ("control_plane", "0043_inventory_read_moment_only_answered")
AFTER = ("control_plane", "0044_capability_rule_per_agent")
KEPT = (
    ("example-agent", "project.delete", "allow"),
    ("example-agent", "project.create", "deny"),
    ("example-reader", "project.delete", "approve"),
)


class CapabilityRulePerAgentMigrationTests(MigrationStepTestCase):
    def setUp(self):
        rules = self.migrate(BEFORE).get_model("control_plane", "CapabilityRule").objects
        for subject, capability, rule in KEPT:
            rules.create(scope="agent", subject=subject, capability=capability, rule=rule)
        for surface in ("mcp", "api"):
            rules.create(scope="surface", subject=surface, capability="project.delete", rule="deny")

    def test_surface_rules_are_gone_and_every_agent_rule_stays(self):
        rules = self.migrate(AFTER).get_model("control_plane", "CapabilityRule").objects

        self.assertEqual(set(rules.values_list("subject", "capability", "rule")), set(KEPT))

    def test_an_agent_holds_one_rule_per_capability(self):
        rules = self.migrate(AFTER).get_model("control_plane", "CapabilityRule").objects

        with self.assertRaises(IntegrityError), transaction.atomic():
            rules.create(subject="example-agent", capability="project.delete", rule="deny")

    def test_going_back_names_every_kept_rule_an_agents(self):
        self.migrate(AFTER)
        rules = self.migrate(BEFORE).get_model("control_plane", "CapabilityRule").objects

        self.assertEqual(
            set(rules.values_list("scope", "subject", "capability", "rule")),
            {("agent", *rule) for rule in KEPT},
        )
