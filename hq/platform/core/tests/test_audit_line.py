"""Each audit event also leaves HQ as one JSON line, once its write commits."""

import json

from django.db import transaction
from django.test import TestCase

from hq.platform.core.audit import record_event
from hq.platform.core.log_lines import LineFormatter
from hq.platform.core.models import AuditLog


class Rollback(Exception):
    pass


class AuditLineTests(TestCase):
    def test_a_committed_event_leaves_one_line_and_a_rolled_back_one_none(self):
        def rolled_back():
            with transaction.atomic():
                record_event(action=AuditLog.Action.UPDATED, type_label="Example", message="Undone")
                raise Rollback

        with self.assertLogs("severino.audit", "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            self.assertRaises(Rollback, rolled_back)
            record_event(
                action=AuditLog.Action.UPDATED,
                type_label="Example",
                message="Changed a record",
                metadata={"actor": "example-agent", "interface": "mcp"},
            )

        (line,) = [json.loads(LineFormatter().format(record))["audit"] for record in logs.records]
        row = AuditLog.objects.get()
        self.assertEqual((line["id"], line["message"], line["actor"]), (row.pk, "Changed a record", "example-agent"))
