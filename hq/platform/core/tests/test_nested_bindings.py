"""Who an operation's rows name is whoever started it: a binding opened inside
cannot replace the actor, and only a held approval's binding rebinds."""

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.security import CLI_ACTOR, Principal, cli_principal
from hq.platform.core.attribution import own_operation
from hq.platform.core.audit import record_operation
from hq.platform.core.bindings import Rebound, audit_operation, operation_context
from hq.platform.core.models import AuditLog

EXAMPLE_PERSON = Principal("example-person", "web", frozenset())


def _example_row(message: str) -> AuditLog:
    record_operation("example.nesting", message, action=AuditLog.Action.UPDATED)
    return AuditLog.objects.get(object_type="example.nesting", message=message)


class ANestedBindingKeepsTheActorTests(TestCase):
    def test_a_shell_binding_inside_a_persons_operation_is_refused_naming_both(self):
        with (
            audit_operation(operation="example.page", principal=EXAMPLE_PERSON),
            self.assertRaises(RuntimeError) as refused,
            audit_operation(operation="example.import", principal=cli_principal()),
        ):
            _example_row("as the shell")

        said = str(refused.exception)
        self.assertIn("web/example-person", said)
        self.assertIn(f"cli/{CLI_ACTOR}", said)
        self.assertFalse(AuditLog.objects.filter(message="as the shell").exists())

    def test_the_same_actor_through_another_door_keeps_the_operation(self):
        with audit_operation(operation="example.job", principal=Principal("example-person", "job", frozenset())):
            outer = _example_row("outer")
            with audit_operation(operation="example.work", principal=EXAMPLE_PERSON):
                inner = _example_row("inner")

        self.assertEqual(inner.operation_id, outer.operation_id)
        self.assertEqual((inner.metadata["interface"], inner.metadata["actor"]), ("web", "example-person"))

    def test_a_door_that_does_not_know_who_is_asking_is_named_by_the_binding_inside(self):
        with (
            operation_context(interface="web", actor="", operation="example.request"),
            audit_operation(operation="example.page", principal=EXAMPLE_PERSON),
        ):
            row = _example_row("named inside")

        self.assertEqual(row.metadata["actor"], "example-person")

    def test_a_door_awaiting_sign_in_inside_an_operation_starts_its_own(self):
        """A request a tool serves in its own process, under the shell's binding."""

        with audit_operation(operation="example.bench", principal=cli_principal()):
            outer = _example_row("the bench")
            with (
                operation_context(interface="web", actor="", operation="example.request"),
                audit_operation(operation="example.page", principal=EXAMPLE_PERSON),
            ):
                served = _example_row("served")

        self.assertNotEqual(served.operation_id, outer.operation_id)
        self.assertEqual(served.metadata["actor"], "example-person")

    def test_only_a_held_approval_rebinds(self):
        with (
            audit_operation(operation="example.approve", principal=EXAMPLE_PERSON),
            own_operation(ApprovalRequest(), operation="example.replay", principal=cli_principal()),
        ):
            row = _example_row("replayed")
            with self.assertRaises(Rebound), audit_operation(operation="example.inside", principal=EXAMPLE_PERSON):
                pass

        self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("cli", CLI_ACTOR))
        with (
            audit_operation(operation="example.approve", principal=EXAMPLE_PERSON),
            self.assertRaises(Rebound),
            own_operation(OperationRequest(), operation="example.replay", principal=cli_principal()),
        ):
            pass
