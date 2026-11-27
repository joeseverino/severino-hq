"""The audit log is append-only by the database's own rule: an UPDATE or DELETE
of a row is refused on every write path but the ones HQ's contract makes."""

from django.db import IntegrityError, connection, transaction
from django.db.utils import DatabaseError
from django.test import TestCase

from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator


class AppendOnlyTests(TestCase):
    def test_no_write_path_changes_or_removes_a_row(self):
        first, second = (operator(username=name) for name in ("example-a", "example-b"))
        row = AuditLog.objects.create(
            action=AuditLog.Action.UPDATED, object_type="Example", message="Changed a record", user=first
        )
        rows = AuditLog.objects.filter(pk=row.pk)
        table = connection.ops.quote_name(AuditLog._meta.db_table)

        def sql(statement: str) -> None:
            with connection.cursor() as cursor:
                cursor.execute(statement, [row.pk])

        row.message = "Something else happened"
        for name, change in (
            ("save", row.save),
            ("update", lambda: rows.update(message="rewritten")),
            ("another actor", lambda: rows.update(user=second)),
            ("a cleared user carrying a change", lambda: rows.update(user=None, message="rewritten")),
            ("delete", rows.delete),
            ("raw update", lambda: sql(f"UPDATE {table} SET message = 'rewritten' WHERE id = %s")),
            ("raw delete", lambda: sql(f"DELETE FROM {table} WHERE id = %s")),
        ):
            with (
                self.subTest(name),
                self.assertRaisesMessage((DatabaseError, IntegrityError), "append-only"),
                transaction.atomic(),
            ):
                change()

        self.assertEqual(rows.values_list("message", "user").get(), ("Changed a record", first.pk))
