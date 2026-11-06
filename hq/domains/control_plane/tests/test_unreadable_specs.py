"""A declared spec its provider's hooks cannot read is said in the log, never dropped in silence."""

from django.test import SimpleTestCase

from hq.platform.application.entity_links import record_name
from hq.platform.application.inventory import record_identity, service_hostnames

KIND = "adguard.rewrite"
READABLE = {"domain": "app.example.com", "answer": "192.0.2.10", "enabled": True}
# A rewrite without the domain its provider's hostnames hook reads.
UNREADABLE = {"answer": "192.0.2.11", "enabled": True}


class UnreadableSpecTests(SimpleTestCase):
    def test_a_spec_the_hook_cannot_read_reads_as_nothing_and_is_logged(self):
        with self.assertLogs("severino.providers", "WARNING") as logged:
            self.assertEqual(service_hostnames(KIND, UNREADABLE), ())

        self.assertEqual([record.event for record in logged.records], ["providers.spec.unreadable"])
        self.assertIn(KIND, logged.output[0])

    def test_a_name_falls_back_to_the_key_and_says_why(self):
        with self.assertLogs("severino.providers", "WARNING") as logged:
            self.assertEqual(record_name(KIND, UNREADABLE, "rewrite-key"), "rewrite-key")

        self.assertEqual([record.event for record in logged.records], ["providers.spec.unreadable"])

    def test_a_readable_spec_logs_nothing(self):
        with self.assertNoLogs("severino.providers", "WARNING"):
            self.assertEqual(service_hostnames(KIND, READABLE), ("app.example.com",))
            self.assertEqual(record_identity(KIND, READABLE), ("app.example.com",))
