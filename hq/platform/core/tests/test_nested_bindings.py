"""Who an operation's rows name is whoever started it: a binding opened inside
cannot replace the actor, and only the host's held changes rebind on purpose."""

import ast

from django.test import SimpleTestCase, TestCase, tag

from hq.platform.application.security import CLI_ACTOR, Principal, cli_principal
from hq.platform.application.tests.tree import ROOT
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


@tag("source", "tree")
class WhoMayRebindTests(SimpleTestCase):
    def test_only_the_approvals_service_passes_rebinds(self):
        """A rebinding anywhere else writes an operation's rows as someone who
        did not start it; the approvals service runs an approved change as its
        requester and lapses an expired one as HQ."""

        passing = [
            f"{relative}:{node.lineno}"
            for path in sorted([*(ROOT / "hq").rglob("*.py"), *(ROOT / "hq_sdk").rglob("*.py")])
            if "/tests/" not in (relative := path.relative_to(ROOT).as_posix())
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.keyword)
            and node.arg == "rebinds"
            and not (isinstance(node.value, ast.Constant) and node.value.value is False)
            and not (isinstance(node.value, ast.Name) and node.value.id == "rebinds")
        ]
        self.assertTrue(passing, "the walk found the approvals service's rebinding")
        self.assertEqual({entry.split(":")[0] for entry in passing}, {"hq/platform/application/approvals.py"})

    def test_only_a_door_binds_nobody_yet(self):
        """A binding naming nobody starts an operation of its own, so code inside
        another's operation that bound one would write its rows as nobody's."""

        doors = {
            "hq/platform/core/attribution.py",
            "hq/platform/core/middleware.py",
            "hq/platform/core/example_estate/doors.py",
            "hq/platform/core/test_runner.py",
        }
        binding = [
            f"{relative}:{node.lineno}"
            for path in sorted([*(ROOT / "hq").rglob("*.py"), *(ROOT / "hq_sdk").rglob("*.py")])
            if "/tests/" not in (relative := path.relative_to(ROOT).as_posix())
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and any(
                keyword.arg == "actor" and isinstance(keyword.value, ast.Constant) and keyword.value.value == ""
                for keyword in node.keywords
            )
        ]
        self.assertTrue(binding, "the walk found the doors")
        self.assertLessEqual({entry.split(":")[0] for entry in binding}, doors, binding)

    def test_only_the_job_runner_and_the_estates_doors_open_a_door_of_their_own(self):
        opening = {
            path.relative_to(ROOT).as_posix()
            for path in sorted([*(ROOT / "hq").rglob("*.py"), *(ROOT / "hq_sdk").rglob("*.py")])
            if "/tests/" not in path.as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "door_of_its_own"
        }
        self.assertEqual(opening, {"hq/domains/jobs/runner.py", "hq/platform/core/example_estate/doors.py"})
