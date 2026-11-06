"""Every refusal code has one status, and every failure of a capability is answered as a refusal.

A code with no row is a defect, answered as a failure of HQ's, and an exception
no handler expected would escape the application.
"""

import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, tag

from hq.platform.core.models import AuditLog
from hq.platform.core.tests import audit_rows
from hq_sdk.capabilities import StrictCommand

from .. import refusal_status
from ..approvals import _UNAPPLIED, TooManyPendingApprovals
from ..capabilities import CapabilitySpec, adapter_refusal, execute_capability
from ..resource_refusals import UnknownResource, UnsupportedResourceOperation
from ..security import AuthorizationError, Capability, Principal, cli_principal
from .facts import TOP, scan

ROOT = Path(__file__).resolve().parents[4]
# Where a code an adapter answers with is written: the application, its doors
# and the domains whose handlers return refusals.
EMITTERS = ("hq/platform/application", "hq/platform/api", "hq/platform/mcp", "hq/domains", "hq_sdk")
# Calls whose first argument, or ``code=``, is a refusal code.
BUILDERS = {"refuse", "_error", "_refusal", "_fail", "Unadmitted", "AgentRefusal", "Limited", "EnvelopeError"}
# The base of the token door's refusals, whose code only its kinds' replace.
NEVER_ANSWERED = {"error"}
SECRET = "/srv/example/secret-path-7f3a"


def _called(names: set[str]) -> dict:
    """The arguments of a call to a function named one of ``names``, by itself or as an attribute."""

    named = f"^({'|'.join(sorted(names))})$"
    return {
        "kind": "argument_list",
        "inside": {
            "kind": "call",
            "field": "arguments",
            "has": {
                "field": "function",
                "any": [
                    {"kind": "identifier", "regex": named},
                    {"kind": "attribute", "has": {"field": "attribute", "regex": named}},
                ],
            },
        },
    }


# Where a code is written: a builder's first positional argument or its
# ``code=``, a class's ``code`` attribute, or a dictionary's "code" entry; and
# each module-level constant an argument may name. Each captures it as $CODE.
RULES = {
    "argument": {
        **_called(BUILDERS - {"_fail", "EnvelopeError"}),
        "has": {
            "nthChild": 1,
            "pattern": "$CODE",
            "not": {
                "any": [{"kind": kind} for kind in ("keyword_argument", "list_splat", "dictionary_splat", "comment")]
            },
        },
    },
    "keyword": {
        "pattern": {"context": "f(code=$CODE)", "selector": "keyword_argument"},
        "inside": _called(BUILDERS),
    },
    "attribute": {
        "any": [{"pattern": "code = $CODE"}, {"pattern": "code: $TYPE = $CODE"}],
        "inside": {
            "kind": "expression_statement",
            "inside": {"kind": "block", "inside": {"kind": "class_definition", "field": "body"}},
        },
    },
    "entry": {"kind": "pair", "regex": "^[\"']code[\"']\\s*:", "has": {"field": "value", "pattern": "$CODE"}},
    "constant": {"pattern": "$NAME = $CODE", **TOP},
}


def codes(*paths: Path) -> list[tuple[str, str]]:
    """Each refusal code written under ``paths``, as where it is and the code."""

    found = [
        each
        for each in scan(RULES, *paths)
        if not {"tests", "migrations"} & set(each.path.parts) and not each.path.name.startswith("test_")
    ]
    constants = {
        (each.path, each.captured["NAME"]): each.value("CODE")
        for each in found
        if each.rule == "constant" and each.captured["NAME"].isidentifier()
    }
    written = []
    for each in found:
        if each.rule == "constant":
            continue
        # A name is read through its module's constant.
        written_as = each.captured["CODE"]
        code = constants.get((each.path, written_as)) if written_as.isidentifier() else each.value("CODE")
        if isinstance(code, str):
            written.append((f"{each.path}:{each.line}", code))
    return written


@tag("source", "tree")
class EveryCodeHasAStatusTests(SimpleTestCase):
    def test_every_code_the_application_answers_with_has_a_row(self):
        found = set()
        for where, code in codes(*(ROOT / package for package in EMITTERS)):
            if code in NEVER_ANSWERED:
                continue
            found.add(code)
            with self.subTest(code=code):
                self.assertIn(
                    code,
                    refusal_status.REFUSAL_STATUS,
                    f"{where} answers {code!r}, which refusal_status.REFUSAL_STATUS gives no status",
                )
        # The walk reads the forms the codes are written in, so it finds the table's own.
        self.assertGreaterEqual(len(found), 17)

    def test_each_form_a_code_is_written_in_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "example.py"
            module.write_text(
                'NAMED = "example_named"\n'
                'class Refused(Exception):\n    code = "example_attribute"\n'
                'class Typed(Exception):\n    code: str = "example_typed"\n'
                'refuse("example_positional", "Said.")\n'
                "self.refuse(NAMED)\n"
                'Limited(code="example_keyword")\n'
                '_fail("Said.", code=NAMED)\n'
                '_fail("example_message")\n'
                "answer = {'ok': False, 'code': 'example_entry'}\n"
                'other("example_other", code="example_other")\n'
                'refuse(f"example_{kind}")\n'
            )

            self.assertEqual(
                sorted(code for _where, code in codes(module)),
                [
                    "example_attribute",
                    "example_entry",
                    "example_keyword",
                    "example_named",
                    "example_named",
                    "example_positional",
                    "example_typed",
                ],
            )

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
