"""A management command a test starts runs inside the shell's door, as
``manage.py`` runs it in production, so a command that binds someone else
inside it fails here rather than there."""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.test import TestCase

from hq.platform.core.audit import record_operation
from hq.platform.core.bindings import Rebound, operation_context
from hq.platform.core.models import AuditLog


class _BindsSomeoneElse(BaseCommand):
    def handle(self, *args, **options):
        with operation_context(interface="cli", actor="someone-else", operation="example.import"):
            record_operation("example.command", "as someone else", action=AuditLog.Action.UPDATED)


class CommandBindingTests(TestCase):
    def test_a_command_that_binds_someone_else_inside_it_is_refused(self):
        with self.assertRaises(Rebound):
            call_command(_BindsSomeoneElse())

        self.assertFalse(AuditLog.objects.filter(message="as someone else").exists())
