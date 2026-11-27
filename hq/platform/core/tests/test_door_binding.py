"""A row names who acted: one written where no door bound anyone is refused,
and a binding opened inside an operation cannot replace its actor.

The test runner binds each test's own thread as the test, so the unbound
write runs on a thread of its own.
"""

import threading

from django.test import TestCase

from hq.platform.application.security import Principal, cli_principal
from hq.platform.core.audit import record_event, record_operation
from hq.platform.core.bindings import audit_operation
from hq.platform.core.models import AuditLog


class UnattributedRowTests(TestCase):
    def test_a_row_with_no_actor_or_another_actor_is_refused(self):
        raised: list[BaseException] = []

        def unbound():
            try:
                record_event(action=AuditLog.Action.UPDATED, message="unbound")
            except RuntimeError as exc:
                raised.append(exc)

        thread = threading.Thread(target=unbound)
        thread.start()
        thread.join()

        person = Principal("example-person", "web", frozenset())
        with (
            audit_operation(operation="example.page", principal=person),
            self.assertRaises(RuntimeError) as rebound,
            audit_operation(operation="example.import", principal=cli_principal()),
        ):
            record_operation("example.door", "as the shell", action=AuditLog.Action.UPDATED)

        self.assertEqual(len(raised), 1)
        self.assertIn("web/example-person", str(rebound.exception))
        self.assertFalse(AuditLog.objects.filter(message__in=("unbound", "as the shell")).exists())
