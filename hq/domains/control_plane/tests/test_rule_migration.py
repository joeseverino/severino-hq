"""A rule set for a client by its id alone follows each person who signed that client in."""

from hq.platform.core.tests.migration_steps import MigrationStepTestCase

KEY_A, KEY_B = "a" * 64, "b" * 64


class RulesFollowEachPersonRowTests(MigrationStepTestCase):
    def test_a_clients_rule_becomes_one_per_person_row_and_a_machines_stays(self):
        apps = self.migrate(
            ("control_plane", "0048_one_pending_approval_per_asker_and_person"),
            ("core", "0034_agent_identity_one_per_client_and_person"),
        )
        AgentIdentity = apps.get_model("core", "AgentIdentity")
        CapabilityRule = apps.get_model("control_plane", "CapabilityRule")
        for person_key in (KEY_A, KEY_B):
            AgentIdentity.objects.create(client_id="example-assistant", person_key=person_key)
        AgentIdentity.objects.create(client_id="example-runner")
        AgentIdentity.objects.create(client_id="example-both")
        AgentIdentity.objects.create(client_id="example-both", person_key=KEY_A)
        for subject in ("example-assistant", "example-runner", "example-both"):
            CapabilityRule.objects.create(subject=subject, capability="project.delete", rule="allow")

        apps = self.migrate(("control_plane", "0049_rules_follow_each_person_row"))

        rules = dict(apps.get_model("control_plane", "CapabilityRule").objects.values_list("subject", "rule"))
        self.assertEqual(
            rules,
            {
                f"example-assistant@{KEY_A}": "allow",
                f"example-assistant@{KEY_B}": "allow",
                "example-runner": "allow",
                "example-both": "allow",
                f"example-both@{KEY_A}": "allow",
            },
        )
