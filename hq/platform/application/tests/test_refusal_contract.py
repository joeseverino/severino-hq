"""Every failure of a capability is answered as a refusal.

A code a plugin's handler answers that the table does not hold is a defect,
answered as a failure of HQ's, and an exception no handler expected would
escape the application.
"""

from dataclasses import dataclass

from django.test import SimpleTestCase, TestCase

from hq.platform.core.models import AuditLog
from hq.platform.core.tests import audit_rows

from .. import refusal_status
from ..approvals import TooManyPendingApprovals
from ..capabilities import CapabilitySpec, adapter_refusal, execute_capability
from ..integration_testing import commands_registered
from ..resource_refusals import UnknownResource, UnsupportedResourceOperation
from ..security import AuthorizationError, Capability, Principal, cli_principal

SECRET = "/srv/example/secret-path-7f3a"


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


@dataclass(frozen=True, slots=True)
class ExampleCommand:
    pass


class CapabilityFailureTests(TestCase):
    def call(self, raised, *, principal=None):
        def handler(command, **_bound):
            raise raised

        spec = CapabilitySpec("example.records.update", "Change.", "read", Capability.READ, ExampleCommand, handler)
        with commands_registered({spec.name: spec}):
            return execute_capability(spec.name, {}, principal=principal or cli_principal())

    def test_an_exception_no_handler_expected_is_operation_failed_and_logged_with_its_traceback(self):
        with self.assertLogs("severino.capabilities", "ERROR") as logged:
            result = self.call(RuntimeError(SECRET))

        self.assertEqual(result["error"]["code"], "operation_failed")
        self.assertNotIn(SECRET, str(result))
        self.assertIsNotNone(logged.records[0].exc_info)

    def test_a_command_the_domain_rejects_is_its_own_code_at_409(self):
        with self.assertLogs("severino.capabilities", "WARNING"):
            result = self.call(ValueError(SECRET))

        self.assertEqual(result["error"]["code"], "rejected")
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
        adapter_refusal("example.records.update", AuthorizationError("Not this agent's."))

        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.DENIED).exists())
