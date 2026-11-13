"""lint:openapi-breaking ends on the errors it fails on, and an exemption says why clients can take the break it admits."""

import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import IO
from unittest.mock import patch

from django.test import SimpleTestCase, tag

from tools.gate.tests.base import fixture_git

# Each form oasdiff matches an --err-ignore line to: an operation's change, a
# shared schema's and a security scheme's, worded as oasdiff 1.37.0's
# checker/localizations_src/en/messages.yaml words them.
ENTRIES = (
    "- in API GET /api/v2/findings/ the `query` request parameter `limit` became required",
    "- in components removed the schema `ExampleRow`",
    "- the security scheme `exampleAuth` was removed from the API",
)


def unexplained(lines: list[str]) -> list[int]:
    # Imported where used, so a ref without the gate reports the failure, not an import error.
    from tools.openapi_breaking import unexplained as checked

    return checked(lines)


class ExemptionReasonTests(SimpleTestCase):
    def test_an_entry_with_its_reason_is_admitted(self):
        for entry in ENTRIES:
            with self.subTest(entry=entry):
                self.assertEqual(unexplained([f"{entry}. Reason: no client sends it yet"]), [])

    def test_an_entry_without_a_reason_is_refused(self):
        for entry in ENTRIES:
            for line in (entry, f"{entry}. Reason:", f"{entry}. Reason:   "):
                with self.subTest(line=line):
                    self.assertEqual(unexplained(["# Admitted breaks", "", line]), [3])

    def test_a_match_oasdiff_makes_in_any_case_needs_a_reason(self):
        # oasdiff lowercases the line before it matches.
        self.assertEqual(unexplained(["- IN COMPONENTS REMOVED THE SCHEMA `ExampleRow`"]), [1])

    def test_prose_oasdiff_cannot_match_needs_no_reason(self):
        self.assertEqual(unexplained(["# Client breaks", "", "An entry is a list item."]), [])


DOCUMENT = "hq/platform/api/hq-api.openapi.json"
EXEMPTIONS = "hq/platform/api/hq-api.breaking-exemptions.md"


def document(operations: range, *, removed: set[int] = frozenset(), trimmed: set[int] = frozenset()) -> str:
    """An API of POST operations whose request takes an optional ``note``: one in ``removed`` is
    gone, which breaks a client (an error), and one in ``trimmed`` no longer takes ``note`` (a warning)."""

    def body(n: int) -> dict:
        properties = {} if n in trimmed else {"note": {"type": "string"}}
        return {"content": {"application/json": {"schema": {"type": "object", "properties": properties}}}}

    paths = {
        f"/api/v2/example/{n}/": {
            "post": {"operationId": f"example{n}", "requestBody": body(n), "responses": {"200": {"description": "ok"}}}
        }
        for n in operations
        if n not in removed
    }
    return json.dumps({"openapi": "3.1.0", "info": {"title": "Example", "version": "1"}, "paths": paths})


@contextmanager
def terminal() -> Iterator[IO[str]]:
    """What the gate prints to its terminal, standard output and error in the order written, as
    fast reads it: the file descriptors, which a program the gate runs writes to, and Python's
    streams, which the test runner may have replaced."""

    with tempfile.TemporaryFile("w+") as said:
        sys.stdout.flush()
        sys.stderr.flush()
        saved = [os.dup(1), os.dup(2)]
        os.dup2(said.fileno(), 1)
        os.dup2(said.fileno(), 2)
        try:
            with redirect_stdout(said), redirect_stderr(said):
                yield said
        finally:
            for number, kept in enumerate(saved, start=1):
                os.dup2(kept, number)
                os.close(kept)


@tag("source")
class VerdictTests(SimpleTestCase):
    """oasdiff judges planted documents in a fixture repository, as the gate runs it on the real one."""

    def judge(self, exemptions: str, **revision: set[int]) -> tuple[int, list[str]]:
        from tools import openapi_breaking

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git = fixture_git(root)
        git("init", "-q")
        (root / DOCUMENT).parent.mkdir(parents=True)
        (root / DOCUMENT).write_text(document(range(12)))
        (root / EXEMPTIONS).write_text(exemptions)
        git("add", ".")
        git("commit", "-q", "--no-verify", "-m", "base")
        base = git("rev-parse", "HEAD")
        (root / DOCUMENT).write_text(document(range(12), **revision))
        with (
            patch.object(openapi_breaking, "ROOT", root),
            patch.object(openapi_breaking, "merge_base", return_value=base),
            terminal() as said,
        ):
            code = openapi_breaking.main([])
            said.flush()
            said.seek(0)
            printed = said.read()
        return code, [line for line in printed.splitlines() if line.strip()]

    def test_the_errors_close_the_run_with_their_count_and_the_warnings_are_only_counted(self):
        # Errors and warnings alternate, as oasdiff orders them by path; one error is exempted.
        exempted = "API POST /api/v2/example/4/ API path removed without deprecation"
        code, lines = self.judge(
            f"- error at {DOCUMENT}, in {exempted}. Reason: no client calls it\n",
            removed={0, 2, 4, 6},
            trimmed={1, 3, 5, 7, 9},
        )
        self.assertEqual(code, 1)
        errors = [n for n, line in enumerate(lines) if line.startswith("error at")]
        self.assertEqual(len(errors), 3)
        self.assertFalse([line for line in lines if exempted in line])
        self.assertFalse([line for line in lines if line.startswith("warning at")])
        self.assertEqual(errors[-1], len(lines) - 2, "only the count closes the run after its last error")
        self.assertIn(" 3 errors", lines[-1])
        warned = [line for line in lines[: errors[0]] if " 5 warnings" in line]
        self.assertEqual(len(warned), 1)
        self.assertIn("oasdiff breaking", warned[0])

    def test_warnings_alone_pass(self):
        code, lines = self.judge("# Admitted breaks\n", trimmed={1, 3})
        self.assertEqual(code, 0)
        self.assertFalse([line for line in lines if line.startswith(("error at", "warning at"))])
        self.assertTrue([line for line in lines if " 2 warnings" in line])
