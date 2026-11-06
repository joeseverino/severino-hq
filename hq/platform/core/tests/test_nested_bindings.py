"""Who an operation's rows name is whoever started it: a binding opened inside
cannot replace the actor, and only the host's held changes rebind on purpose."""

from django.test import TestCase

from hq.platform.application.security import CLI_ACTOR, Principal, cli_principal
from hq.platform.core.audit import audit_operation, operation_context, record_operation
from hq.platform.core.models import AuditLog
from hq_sdk import audit as sdk_audit

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

    def test_a_plugins_binding_inside_a_persons_operation_is_refused(self):
        with (
            audit_operation(operation="example.page", principal=EXAMPLE_PERSON),
            self.assertRaises(RuntimeError),
            sdk_audit.operation_context(interface="cli", actor=CLI_ACTOR, operation="example.import"),
        ):
            pass

    def test_a_plugin_cannot_ask_to_rebind(self):
        # Spread, since the SDK's signatures leave the keyword out.
        asking = {"rebinds": True}
        with self.assertRaises(TypeError):
            sdk_audit.audit_operation(operation="example.import", principal=cli_principal(), **asking)
        with self.assertRaises(TypeError):
            sdk_audit.operation_context(interface="cli", actor=CLI_ACTOR, operation="example.import", **asking)

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

    def test_a_plugins_binding_must_name_who_acts(self):
        with (
            self.assertRaises(ValueError),
            sdk_audit.operation_context(interface="web", actor="", operation="example.import"),
        ):
            pass

    def test_the_host_rebinds_only_when_it_says_so(self):
        with (
            audit_operation(operation="example.approve", principal=EXAMPLE_PERSON),
            audit_operation(operation="example.replay", principal=cli_principal(), rebinds=True),
        ):
            row = _example_row("replayed")

        self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("cli", CLI_ACTOR))
