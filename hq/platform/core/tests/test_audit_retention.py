"""Routine audit events expire after the days the setting names, and the prune is on the record."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from ..audit import CONNECTION_AUDIT_TYPE, prune_routine
from ..models import AuditLog


def _event(action, object_type, *, days_old):
    return AuditLog.objects.create(
        action=action, object_type=object_type, created_at=timezone.now() - timedelta(days=days_old)
    )


class PruneTests(TestCase):
    def test_a_retention_under_a_day_is_refused(self):
        with self.assertRaises(ValueError):
            prune_routine(days=0)

    @override_settings(SEVERINO_AUDIT_ROUTINE_DAYS=10)
    def test_the_command_deletes_what_the_setting_says_is_old_and_records_it(self):
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=11)
        _event(AuditLog.Action.OBSERVED, CONNECTION_AUDIT_TYPE, days_old=9)

        call_command("prune_audit", "--check-only", stdout=StringIO())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.OBSERVED).count(), 2)

        call_command("prune_audit", stdout=StringIO())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.OBSERVED).count(), 1)
        # The prune is itself on the record, and not routine.
        self.assertTrue(AuditLog.objects.filter(object_type="audit.prune", action=AuditLog.Action.DELETED).exists())
