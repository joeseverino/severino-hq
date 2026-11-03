"""A retired capability leaves no standing rule and no call held under it;
every other capability's rows stay as they were."""

from datetime import timedelta

from django.utils import timezone

from hq.platform.core.tests.migration_steps import MigrationStepTestCase

BEFORE = ("control_plane", "0039_capability_names_follow_the_pattern")
AFTER = ("control_plane", "0040_contact_submission_list_retired")
RETIRED = "contact.submission.list"
KEPT = "contact.submission.delete"


class RetiredCapabilityMigrationTests(MigrationStepTestCase):
    def setUp(self):
        apps = self.migrate(BEFORE)
        rules = apps.get_model("control_plane", "CapabilityRule").objects
        for capability, rule in ((RETIRED, "deny"), (KEPT, "approve")):
            rules.create(scope="agent", subject="example-agent", capability=capability, rule=rule)
        rules.create(scope="surface", subject="mcp", capability=RETIRED, rule="deny")
        held = apps.get_model("control_plane", "ApprovalRequest").objects
        now = timezone.now()
        common = {
            "target": "1",
            "requested_actor": "example-agent",
            "requested_interface": "mcp",
            "expires_at": now + timedelta(days=1),
        }
        self.pending = held.create(capability=RETIRED, content_fingerprint="retired", **common).pk
        self.kept = held.create(capability=KEPT, content_fingerprint="kept", **common).pk
        self.settled = held.create(
            capability=RETIRED,
            content_fingerprint="settled",
            state="approved",
            decided_actor="example-person",
            decided_interface="web",
            decided_at=now,
            **common,
        ).pk

    def test_a_rule_for_it_is_gone_and_every_other_rule_stays(self):
        rules = self.migrate(AFTER).get_model("control_plane", "CapabilityRule").objects
        self.assertEqual(
            list(rules.values_list("scope", "subject", "capability", "rule")),
            [("agent", "example-agent", KEPT, "approve")],
        )

    def test_a_call_held_under_it_is_settled_and_every_other_call_stays(self):
        held = self.migrate(AFTER).get_model("control_plane", "ApprovalRequest").objects
        states = dict(held.values_list("pk", "state"))
        self.assertEqual(
            (states[self.pending], states[self.kept], states[self.settled]),
            ("stale", "pending", "approved"),
        )
        self.assertIsNotNone(held.get(pk=self.pending).decided_at)
