"""Every refusal code has one status, and every failure of a capability is answered as a refusal.

A code with no row is a defect, answered as a failure of HQ's, and an exception
no handler expected would escape the application.
"""

import ast
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, tag

from hq.platform.core.models import AuditLog
from hq.platform.core.tests import audit_rows
from hq_sdk.capabilities import StrictCommand

from .. import refusal_status
from ..approvals import _UNAPPLIED, TooManyPendingApprovals
from ..capabilities import CapabilitySpec, adapter_refusal, execute_capability
from ..resources import UnknownResource, UnsupportedResourceOperation
from ..security import AuthorizationError, Capability, Principal, cli_principal
from .source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]
# Where a code an adapter answers with is written: the application, its doors
# and the domains whose handlers return refusals.
EMITTERS = ("hq/platform/application", "hq/platform/api", "hq/platform/mcp", "hq/domains", "hq_sdk")
# Calls whose first argument, or ``code=``, is a refusal code.
BUILDERS = {"refuse", "_error", "_refusal", "_fail", "Unadmitted", "AgentRefusal", "Limited", "EnvelopeError"}
# The base of the token door's refusals, whose code only its kinds' replace.
NEVER_ANSWERED = {"error"}
SECRET = "/srv/example/secret-path-7f3a"


def _string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _class_codes(node: ast.ClassDef):
    for statement in node.body:
        targets = statement.targets if isinstance(statement, ast.Assign) else []
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        if any(isinstance(target, ast.Name) and target.id == "code" for target in targets):
            yield statement.lineno, _string(statement.value)


def _call_codes(node: ast.Call, resolved):
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    if name not in BUILDERS:
        return
    if node.args and name not in {"_fail", "EnvelopeError"}:
        yield node.lineno, resolved(node.args[0])
    for keyword in node.keywords:
        if keyword.arg == "code":
            yield node.lineno, resolved(keyword.value)


def _codes(tree: ast.Module):
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and _string(node.value) is not None
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def resolved(node):
        return constants.get(node.id) if isinstance(node, ast.Name) else _string(node)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield from _class_codes(node)
        elif isinstance(node, ast.Call):
            yield from _call_codes(node, resolved)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is not None and _string(key) == "code" and _string(value) is not None:
                    yield node.lineno, _string(value)


@tag("source", "tree")
class EveryCodeHasAStatusTests(SimpleTestCase):
    def test_every_code_the_application_answers_with_has_a_row(self):
        found = set()
        for package in EMITTERS:
            for path in source_files(ROOT / package, "*.py"):
                if "tests" in path.parts or "migrations" in path.parts or path.name.startswith("test_"):
                    continue
                for line, code in _codes(parsed(path)):
                    if code is None or code in NEVER_ANSWERED:
                        continue
                    found.add(code)
                    with self.subTest(code=code):
                        self.assertIn(
                            code,
                            refusal_status.REFUSAL_STATUS,
                            f"{path.relative_to(ROOT)}:{line} answers {code!r}, which "
                            "refusal_status.REFUSAL_STATUS gives no status",
                        )
        # The walk reads the forms the codes are written in, so it finds the table's own.
        self.assertGreaterEqual(len(found), 17)

    def test_a_held_call_that_ends_unapplied_answers_a_code_with_a_row(self):
        for code, _said in _UNAPPLIED.values():
            self.assertIn(code, refusal_status.REFUSAL_STATUS)


class UnlistedCodeTests(SimpleTestCase):
    def test_a_code_with_no_row_is_logged_and_answered_as_a_failure_that_names_none_of_it(self):
        sent = {"ok": False, "error": {"code": "example_unlisted", "message": SECRET, "details": SECRET}}

        with self.assertLogs("severino.capabilities", "ERROR") as logged:
            refusal = refusal_status.refusal_of(sent)

        self.assertEqual(refusal.status, 500)
        self.assertEqual(refusal.answer()["error"]["code"], "operation_failed")
        self.assertNotIn("example_unlisted", str(refusal.answer()))
        self.assertNotIn(SECRET, str(refusal.answer()))
        self.assertIn("example_unlisted", logged.output[0])


class ExampleCommand(StrictCommand):
    pass


class CapabilityFailureTests(TestCase):
    def call(self, raised, *, principal=None):
        def handler(command, **_bound):
            raise raised

        spec = CapabilitySpec("example.records.change", "Change.", "read", Capability.READ, ExampleCommand, handler)
        with mock.patch("hq.platform.application.capabilities.capability_registry", return_value={spec.name: spec}):
            return execute_capability(spec.name, {}, principal=principal or cli_principal())

    def test_an_exception_no_handler_expected_is_operation_failed_and_logged_with_its_traceback(self):
        with self.assertLogs("severino.capabilities", "ERROR") as logged:
            result = self.call(RuntimeError(SECRET))

        self.assertEqual(result["error"]["code"], "operation_failed")
        self.assertEqual(refusal_status.REFUSAL_STATUS["operation_failed"], 500)
        self.assertNotIn(SECRET, str(result))
        self.assertIsNotNone(logged.records[0].exc_info)

    def test_a_command_the_domain_rejects_is_its_own_code_at_409(self):
        with self.assertLogs("severino.capabilities", "WARNING"):
            result = self.call(ValueError(SECRET))

        self.assertEqual(result["error"]["code"], "rejected")
        self.assertEqual(refusal_status.REFUSAL_STATUS["rejected"], 409)
        self.assertNotIn(SECRET, str(result))

    def test_a_resource_failure_is_the_code_the_api_answers(self):
        for raised, code in (
            (UnknownResource("No such list."), "unknown_resource"),
            (UnsupportedResourceOperation("That list has no detail."), "unsupported_operation"),
        ):
            with self.subTest(code):
                self.assertEqual(self.call(raised)["error"]["code"], code)

    def test_a_denial_or_a_hold_that_could_not_wait_is_recorded_once(self):
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        for raised in (AuthorizationError("Not this agent's."), TooManyPendingApprovals("Answer those first.")):
            with self.subTest(type(raised).__name__):
                before = audit_rows.mark()

                self.call(raised, principal=agent)

                row = audit_rows.since(before).get(action=AuditLog.Action.DENIED)
                self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("mcp", "example-agent"))
                self.assertEqual(row.metadata["reason"], raised.code)

    def test_a_refusal_mapped_without_its_caller_records_nothing(self):
        adapter_refusal("example.records.change", AuthorizationError("Not this agent's."))

        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.DENIED).exists())
