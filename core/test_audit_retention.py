"""Audit events name their connection; routine ones expire and nothing else does."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .audit import (
    CONNECTION_AUDIT_TYPE,
    ROUTINE_EVENTS,
    SECURITY_ACTIONS,
    audit_connection,
    last_activity,
    prune_routine,
    record_event,
)
from .models import AuditLog


def _event(action, object_type="", *, days_old=0, connection="", user=None, message=""):
    event = AuditLog.objects.create(
        action=action,
        object_type=object_type,
        connection=connection,
        user=user,
        message=message,
    )
    if days_old:
        AuditLog.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(days=days_old)
        )
    return event


class ClassificationTests(TestCase):
    def test_no_security_action_is_routine(self):
        for action, _ in ROUTINE_EVENTS:
            self.assertNotIn(action, SECURITY_ACTIONS)

    def test_the_security_record_is_never_routine(self):
        for action in (
            AuditLog.Action.LOGIN,
            AuditLog.Action.LOGIN_FAILED,
            AuditLog.Action.DENIED,
            AuditLog.Action.SETTINGS_CHANGED,
            AuditLog.Action.DELETED,
            AuditLog.Action.CREATED,
            AuditLog.Action.UPDATED,
        ):
            self.assertIn(action, SECURITY_ACTIONS)
            self.assertFalse(any(routine == action for routine, _ in ROUTINE_EVENTS))

    def test_every_action_is_classified(self):
        routine = {action for action, _ in ROUTINE_EVENTS}
        for action in AuditLog.Action:
            self.assertTrue(action in SECURITY_ACTIONS or action in routine, action)


class PruneTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )

    def test_only_old_routine_machine_events_are_deleted(self):
        old = _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=40)
        recent = _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=5)
        by_person = _event(
            AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=40, user=self.user
        )
        other_type = _event(AuditLog.Action.OBSERVED, "Managed resource", days_old=40)
        kept = [
            _event(action, CONNECTION_AUDIT_TYPE, days_old=400)
            for action in SECURITY_ACTIONS
        ]

        deleted = prune_routine(days=30)

        self.assertEqual(deleted, 1)
        self.assertFalse(AuditLog.objects.filter(pk=old.pk).exists())
        for event in (recent, by_person, other_type, *kept):
            self.assertTrue(AuditLog.objects.filter(pk=event.pk).exists())

    def test_deletes_in_batches(self):
        for _ in range(5):
            _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=40)

        self.assertEqual(prune_routine(days=30, batch=2), 5)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.OBSERVED).exists())

    def test_check_only_counts_and_deletes_nothing(self):
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=40)

        self.assertEqual(prune_routine(days=30, check_only=True), 1)
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_a_retention_under_a_day_is_refused(self):
        with self.assertRaises(ValueError):
            prune_routine(days=0)

    @override_settings(SEVERINO_AUDIT_ROUTINE_DAYS=10)
    def test_the_command_uses_the_setting_and_prints_counts(self):
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=11)
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=9)

        out = StringIO()
        call_command("prune_audit", "--check-only", stdout=out)
        self.assertEqual(
            out.getvalue().strip(),
            "1 routine event older than 10 days would be deleted.",
        )
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.OBSERVED).count(), 2)

        out = StringIO()
        call_command("prune_audit", stdout=out)
        self.assertEqual(out.getvalue().strip(), "1 routine event older than 10 days deleted.")
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.OBSERVED).count(), 1)
        # The prune is itself on the record, and not routine.
        self.assertTrue(
            AuditLog.objects.filter(
                object_type="audit.prune", action=AuditLog.Action.DELETED
            ).exists()
        )


class ConnectionAttributionTests(TestCase):
    def test_an_event_names_its_connection(self):
        event = record_event(action=AuditLog.Action.UPDATED, connection="example-dns")

        self.assertEqual(event.connection, "example-dns")

    def test_a_block_names_the_connection_of_its_events(self):
        with audit_connection("example-api"):
            inside = record_event(action=AuditLog.Action.UPDATED)
        outside = record_event(action=AuditLog.Action.UPDATED)

        self.assertEqual(inside.connection, "example-api")
        self.assertEqual(outside.connection, "")

    def test_an_explicit_connection_wins_over_the_block(self):
        with audit_connection("example-api"):
            event = record_event(action=AuditLog.Action.UPDATED, connection="example-dns")

        self.assertEqual(event.connection, "example-dns")


class LastActivityTests(TestCase):
    def test_the_latest_event_per_connection_in_one_query(self):
        for ref in ("a", "b", "c"):
            _event(AuditLog.Action.UPDATED, connection=ref, days_old=3, message="older")
            _event(AuditLog.Action.UPDATED, connection=ref, message=f"latest {ref}")

        with self.assertNumQueries(1):
            found = last_activity(["a", "b", "c", "none"])

        self.assertEqual(set(found), {"a", "b", "c"})
        self.assertEqual(found["b"].message, "latest b")

    def test_after_a_prune_the_latest_remaining_event_is_shown(self):
        kept = _event(AuditLog.Action.UPDATED, connection="a", days_old=60, message="kept")
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, connection="a", days_old=40)
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, connection="b", days_old=40)

        prune_routine(days=30)
        found = last_activity(["a", "b"])

        self.assertEqual(found["a"].pk, kept.pk)
        self.assertNotIn("b", found)

    def test_no_connections_find_nothing(self):
        _event(AuditLog.Action.UPDATED, connection="a")

        with self.assertNumQueries(0):
            self.assertEqual(last_activity([""]), {})

    def test_only_the_named_connections_are_grouped(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        _event(AuditLog.Action.UPDATED, connection="a")
        _event(AuditLog.Action.UPDATED, connection="other")

        with CaptureQueriesContext(connection) as captured:
            found = last_activity(["a"])

        self.assertEqual(set(found), {"a"})
        (query,) = captured.captured_queries
        self.assertRegex(query["sql"], r'"connection" IN \(')
