"""A capability's standing rule and pending approval follow it to its new name;
a settled approval keeps the name it was decided under."""

from datetime import timedelta

from django.utils import timezone

from hq.platform.application.approvals import fingerprint
from hq.platform.core.tests.migration_steps import MigrationStepTestCase

BEFORE = ("control_plane", "0038_approval_key_names_its_asker")
AFTER = ("control_plane", "0039_capability_names_follow_the_pattern")


class CapabilityRenameMigrationTests(MigrationStepTestCase):
    def setUp(self):
        apps = self.migrate(BEFORE)
        apps.get_model("control_plane", "CapabilityRule").objects.create(
            scope="agent", subject="example-agent", capability="infrastructure.restart", rule="deny"
        )
        held = apps.get_model("control_plane", "ApprovalRequest")
        now = timezone.now()
        common = {
            "target": "example-key",
            "payload": {"reason": "example"},
            "baseline": {"generation": 1},
            "requested_actor": "example-agent",
            "requested_interface": "mcp",
            "expires_at": now + timedelta(days=1),
        }
        self.pending = held.objects.create(
            capability="infrastructure.restart",
            content_fingerprint=fingerprint("infrastructure.restart", "example-key", *self._call(common)),
            **common,
        ).pk
        self.settled = held.objects.create(
            capability="infrastructure.restart",
            content_fingerprint="settled",
            state="approved",
            decided_actor="example-person",
            decided_interface="web",
            decided_at=now,
            **common,
        ).pk

    @staticmethod
    def _call(row):
        return row["payload"], row["baseline"]

    def test_a_rule_moves_to_the_new_name(self):
        rules = self.migrate(AFTER).get_model("control_plane", "CapabilityRule").objects
        self.assertEqual(
            list(rules.values_list("capability", "rule")),
            [("infrastructure.resource.restart", "deny")],
        )

    def test_a_pending_approval_moves_and_still_matches_its_call(self):
        held = self.migrate(AFTER).get_model("control_plane", "ApprovalRequest").objects.get(pk=self.pending)
        self.assertEqual(held.capability, "infrastructure.resource.restart")
        self.assertEqual(
            held.content_fingerprint,
            fingerprint(held.capability, held.target, held.payload, held.baseline),
        )

    def test_a_settled_approval_keeps_its_name(self):
        held = self.migrate(AFTER).get_model("control_plane", "ApprovalRequest").objects.get(pk=self.settled)
        self.assertEqual((held.capability, held.content_fingerprint), ("infrastructure.restart", "settled"))
