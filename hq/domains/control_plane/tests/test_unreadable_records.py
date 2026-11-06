"""A swept record its provider cannot read is said in the log, never dropped in silence."""

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.application.adoption import unmanaged
from hq.platform.application.inventory import live_spec

KIND = "adguard.rewrite"
READABLE = {"domain": "app.example.com", "answer": "192.0.2.10", "enabled": True}
# A rewrite the controller reported without the answer the provider's model needs.
UNREADABLE = {"domain": "tool.example.com", "enabled": True}


class UnreadableRecordTests(TestCase):
    def setUp(self):
        ProviderInventory.objects.create(
            kind=KIND, records=[READABLE, UNREADABLE], observed_at=timezone.now(), reachable=True
        )

    def test_the_unmanaged_list_keeps_the_rest_and_logs_the_one_it_cannot_read(self):
        with self.assertLogs("severino.providers", "WARNING") as logged:
            found = unmanaged()

        self.assertEqual([item.hostnames for item in found if item.kind == KIND], [("app.example.com",)])
        self.assertEqual(len(logged.records), 1)
        self.assertIn(KIND, logged.output[0])

    def test_a_live_spec_lookup_logs_the_record_it_cannot_read(self):
        with self.assertLogs("severino.providers", "WARNING") as logged:
            self.assertIsNone(live_spec(KIND, {"domain": "elsewhere.example.com", "answer": "192.0.2.11"}))

        self.assertIn(KIND, logged.output[0])
