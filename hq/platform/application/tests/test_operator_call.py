"""The operator's command line answers a refusal as the doors do: the ``{"ok": false}``
answer on stdout and a failing exit, for a refusal, JSON the API refuses, or a failure."""

import json
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from hq.platform.mcp.server import mcp

SECRET = "/srv/example/secret-path-7f3a"


def _called(raw):
    """What the command wrote on stdout for ``raw``, and the error it exited with, if any."""

    output = StringIO()
    failed = None
    with mock.patch("sys.stdin", StringIO(raw)):
        try:
            call_command("hq_call", stdout=output)
        except CommandError as error:
            failed = error
    return json.loads(output.getvalue()) if output.getvalue() else None, failed


class OperatorCallTests(TestCase):
    def test_a_refusal_is_answered_on_stdout_and_fails(self):
        request = {"tool": "execute_capability", "arguments": {"name": "example.missing", "payload": {}}}

        answer, failed = _called(json.dumps(request))

        self.assertEqual(answer["ok"], False)
        self.assertEqual(answer["error"]["code"], "unknown_capability")
        self.assertIsNotNone(failed)

    def test_json_the_api_refuses_is_refused_as_the_api_refuses_it(self):
        for raw in (
            '{"tool": "describe_capabilities", "tool": "audit_registry"}',
            '{"tool": "describe_capabilities", "arguments": {"name": "example", "name": "other"}}',
            '{"tool": "describe_capabilities", "arguments": {"limit": NaN}}',
            "not json",
        ):
            with self.subTest(raw=raw):
                answer, failed = _called(raw)

                self.assertEqual(answer and answer["error"]["code"], "invalid_json")
                self.assertIsNotNone(failed)

    def test_an_unexpected_failure_answers_as_the_doors_do_and_says_nothing_of_it(self):
        async def broken(_arguments):
            raise RuntimeError(f"could not open {SECRET}")

        tool = SimpleNamespace(name="audit_registry", call=broken)
        with mock.patch.dict(mcp.tools, {"audit_registry": tool}), self.assertLogs("severino.capabilities"):
            answer, failed = _called(json.dumps({"tool": "audit_registry"}))

        self.assertEqual(answer and answer["error"]["code"], "operation_failed")
        self.assertIsNotNone(failed)
        self.assertNotIn(SECRET, json.dumps(answer) + str(failed))

    def test_an_answer_is_written_and_succeeds(self):
        answer, failed = _called(json.dumps({"tool": "audit_registry"}))

        self.assertIsNone(failed)
        self.assertIn("orphan_projects", answer)
