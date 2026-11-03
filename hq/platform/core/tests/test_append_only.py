"""The audit log is append-only by the database's own rule: an UPDATE or DELETE
of a row is refused on every write path but the three HQ's contract makes."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.utils import DatabaseError
from django.test import TestCase
from django.utils import timezone

from hq.platform.application import denials
from hq.platform.core.audit import ROUTINE_EVENTS, prune_routine, record_event
from hq.platform.core.models import AuditLog

REFUSED = (DatabaseError, IntegrityError)


def _row(**fields) -> AuditLog:
    return AuditLog.objects.create(
        **{"action": AuditLog.Action.UPDATED, "object_type": "Example", "message": "Changed a record", **fields}
    )


def _sql(statement: str, *params) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


class RefusedTests(TestCase):
    """What nothing may do to a row once it is written."""

    def assertRefused(self, change) -> None:
        with self.assertRaisesMessage(REFUSED, "append-only"), transaction.atomic():
            change()

    def test_a_row_cannot_be_saved_again(self):
        row = _row()
        row.message = "Something else happened"

        self.assertRefused(row.save)

    def test_no_queryset_update_or_delete_reaches_a_row(self):
        row = _row()

        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).update(message="rewritten"))
        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).update(created_at=timezone.now()))
        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).delete())
        self.assertEqual(AuditLog.objects.get(pk=row.pk).message, "Changed a record")

    def test_raw_sql_is_refused_as_the_orm_is(self):
        row = _row()
        table = connection.ops.quote_name(AuditLog._meta.db_table)

        self.assertRefused(lambda: _sql(f"UPDATE {table} SET message = 'rewritten' WHERE id = %s", row.pk))
        self.assertRefused(lambda: _sql(f"DELETE FROM {table} WHERE id = %s", row.pk))
        self.assertRefused(lambda: _sql(f"DELETE FROM {table}"))

    def test_who_acted_cannot_be_moved_onto_someone_else(self):
        first, second = (get_user_model().objects.create_user(name) for name in ("example-a", "example-b"))
        row = _row(user=first)

        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).update(user=second))

    def test_clearing_the_user_cannot_carry_another_change(self):
        row = _row(user=get_user_model().objects.create_user("example-a"))

        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).update(user=None, message="rewritten"))

    def test_a_routine_event_a_user_made_is_kept(self):
        action, object_type = min(ROUTINE_EVENTS)
        row = _row(action=action, object_type=object_type, user=get_user_model().objects.create_user("example-a"))

        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).delete())

    def test_a_refusal_is_recounted_only_upward_and_only_its_count(self):
        row = _row(action=AuditLog.Action.DENIED, metadata={"actor": "example-agent", "reason": "x", "count": 3})
        later = timezone.now().isoformat()

        def counted(**metadata):
            return lambda: AuditLog.objects.filter(pk=row.pk).update(metadata={**row.metadata, **metadata})

        self.assertRefused(counted(count=2))
        self.assertRefused(counted(count="9"))
        self.assertRefused(counted(count=4, reason="something else"))
        self.assertRefused(counted(count=4, actor="example-other"))
        counted(count=4, last_at=later)()
        row.refresh_from_db()
        self.assertRefused(counted(count=5, last_at=(timezone.now() - timedelta(days=1)).isoformat()))
        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).update(metadata={"count": 9}))

    def test_only_a_refusal_is_recounted(self):
        row = _row(metadata={"count": 1})

        self.assertRefused(lambda: AuditLog.objects.filter(pk=row.pk).update(metadata={"count": 2}))


class AllowedTests(TestCase):
    """The changes HQ's own contract makes."""

    def test_the_prune_removes_routine_events_and_nothing_else(self):
        action, object_type = min(ROUTINE_EVENTS)
        old = timezone.now() - timedelta(days=30)
        routine = _row(action=action, object_type=object_type, created_at=old)
        kept = _row(created_at=old)

        self.assertEqual(prune_routine(days=7), 1)

        self.assertFalse(AuditLog.objects.filter(pk=routine.pk).exists())
        self.assertTrue(AuditLog.objects.filter(pk=kept.pk).exists())

    def test_a_deleted_user_leaves_their_rows_saying_who_they_were(self):
        user = get_user_model().objects.create_user("example-operator")
        row = record_event(action=AuditLog.Action.UPDATED, type_label="Example", user=user)

        user.delete()

        row.refresh_from_db()
        self.assertIsNone(row.user_id)
        self.assertEqual((row.actor, row.actor_label), ("example-operator", "example-operator"))

    def test_a_coalesced_refusal_counts_every_call(self):
        for _ in range(3):
            denials.record_denial(interface="mcp", reason="invalid_credential", source="192.0.2.7", authenticated=False)

        row = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertEqual(row.metadata["count"], 3)


class InstalledTests(TestCase):
    """The guard stands on the migrated database as the code generates it today."""

    def test_the_database_holds_the_triggers_the_code_generates(self):
        from hq.platform.core import append_only

        self.assertEqual(append_only.installed(), append_only.triggers())

    def test_the_recount_rule_names_the_keys_the_refusals_move(self):
        from hq.platform.core import append_only

        update = next(sql for name, sql in append_only.triggers().items() if name.endswith("update"))

        for key in (denials.COUNT, denials.LAST_AT):
            self.assertIn(f"'$.{key}'", update)

    def test_a_reinstall_puts_back_what_a_rebuilt_table_lost(self):
        from hq.platform.core import append_only, revisions

        append_only.drop()
        self.assertEqual(append_only.installed(), {})

        revisions.install()

        self.assertEqual(append_only.installed(), append_only.triggers())
