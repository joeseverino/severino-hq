"""A management command a test starts runs inside the shell's door, as
``manage.py`` runs it in production, so a command that binds someone else
inside it fails here rather than there."""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.test import SimpleTestCase, TestCase

from hq.platform.application.security import CLI_ACTOR, CLI_INTERFACE
from hq.platform.core.attribution import Rebound, bound
from hq.platform.core.audit import operation_context, record_operation
from hq.platform.core.models import AuditLog
from hq_sdk import audit as sdk_audit


class _Writes(BaseCommand):
    def handle(self, *args, **options):
        record_operation("example.command", "written", action=AuditLog.Action.UPDATED)


class _BindsSomeoneElse(BaseCommand):
    def handle(self, *args, **options):
        with sdk_audit.operation_context(interface="cli", actor="someone-else", operation="example.import"):
            record_operation("example.command", "as someone else", action=AuditLog.Action.UPDATED)


_SEEN: list = []


class _ReadsItsBinding(BaseCommand):
    requires_system_checks = ()

    def handle(self, *args, **options):
        _SEEN.append(bound())


def _written() -> AuditLog:
    return AuditLog.objects.get(object_type="example.command", message="written")


class CommandBindingTests(TestCase):
    def test_a_called_command_writes_as_the_local_operator_through_the_shells_door(self):
        call_command(_Writes())

        written = _written()
        self.assertEqual(written.metadata["interface"], CLI_INTERFACE)
        self.assertEqual(written.metadata["actor"], CLI_ACTOR)

    def test_a_command_that_binds_someone_else_inside_it_is_refused(self):
        with self.assertRaises(Rebound):
            call_command(_BindsSomeoneElse())

        self.assertFalse(AuditLog.objects.filter(message="as someone else").exists())

    def test_a_command_started_inside_a_door_keeps_that_doors_binding(self):
        with operation_context(interface="job", actor="example-person", operation="example.job"):
            call_command(_Writes())

        written = _written()
        self.assertEqual(written.metadata["interface"], "job")
        self.assertEqual(written.metadata["actor"], "example-person")


class CommandFromItsArgumentsTests(SimpleTestCase):
    # run_from_argv closes every connection after the command, as manage.py's
    # process ends, so this case reads the binding and touches no database.
    def test_a_command_run_from_its_arguments_is_the_local_operators(self):
        _SEEN.clear()
        _ReadsItsBinding().run_from_argv(["manage.py", "example_command"])

        [seen] = _SEEN
        self.assertEqual((seen.interface, seen.actor), (CLI_INTERFACE, CLI_ACTOR))
