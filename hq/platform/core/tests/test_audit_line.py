"""Each audit event also leaves HQ as one JSON line, once its write commits."""

import json

from django.db import transaction
from django.test import TestCase

from hq.platform.application import denials
from hq.platform.core.audit import record_event
from hq.platform.core.logging import JsonFormatter
from hq.platform.core.models import AuditLog

AUDIT_LOGGER = "severino.audit"


class Rollback(Exception):
    pass


class AuditLineTests(TestCase):
    def lines(self, write):
        """The JSON lines ``write`` leaves on the audit logger once it commits."""

        with self.assertLogs(AUDIT_LOGGER, "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            write()
        return [json.loads(JsonFormatter().format(record)) for record in logs.records]

    def test_a_committed_event_writes_one_line_carrying_its_row(self):
        from hq.platform.core import tracing

        minted = self.enterContext(tracing.trace("POST /example/"))
        (line,) = self.lines(
            lambda: record_event(
                action=AuditLog.Action.UPDATED,
                type_label="Example",
                message="Changed a record",
                metadata={"actor": "example-agent", "interface": "mcp"},
            )
        )
        row = AuditLog.objects.get()

        audit = line["audit"]
        self.assertEqual(audit["id"], row.pk)
        self.assertEqual(
            (audit["action"], audit["object_type"], audit["message"]), ("updated", "Example", "Changed a record")
        )
        self.assertEqual((audit["actor"], audit["interface"]), ("example-agent", "mcp"))
        self.assertEqual(audit["metadata"], row.metadata)
        self.assertEqual(line["request_id"], minted)
        self.assertEqual(audit["operation_id"], row.operation_id)

    def test_a_rolled_back_event_writes_no_line(self):
        def rolled_back():
            with transaction.atomic():
                record_event(action=AuditLog.Action.UPDATED, type_label="Example")
                raise Rollback

        with self.assertNoLogs(AUDIT_LOGGER), self.captureOnCommitCallbacks(execute=True):
            self.assertRaises(Rollback, rolled_back)

        self.assertFalse(AuditLog.objects.exists())

    def test_a_recounted_refusal_writes_its_new_count(self):
        def refused():
            denials.record_denial(interface="mcp", reason="invalid_credential", source="192.0.2.7", authenticated=False)

        refused()
        lines = self.lines(refused)
        row = AuditLog.objects.get(action=AuditLog.Action.DENIED)

        self.assertEqual(
            [(line["event"], line["audit"]["id"], line["audit"][denials.COUNT]) for line in lines],
            [("audit.recounted", row.pk, 2)],
        )
