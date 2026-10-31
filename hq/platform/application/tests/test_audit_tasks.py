"""The audit: tasks stay reads, since a read-only auditor may run any of them."""

import os
import re
import subprocess
from typing import Any

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT, body, mise_tasks

# A caller's words reach a task only through what usage declares: passed-through
# arguments and templating would let one add a flag that writes.
_PASSED_THROUGH = re.compile(r'"?\$[@*]"?|\{\{')


def refusals(task: dict[str, Any]) -> list[str]:
    """Why a caller could change what the task runs, or the task is listed; empty when neither."""

    found = ["passes its arguments through"] if _PASSED_THROUGH.search(body(task)) else []
    if task["depends"]:
        found.append("depends on another task")
    if not task["hide"]:
        found.append("is listed by `mise tasks`")
    return found


@tag("source")
class AuditTaskTests(SimpleTestCase):
    def test_every_audit_task_only_reads(self) -> None:
        audits = {name: task for name, task in mise_tasks().items() if name.startswith("audit:")}
        self.assertTrue(audits, "no audit: task found, so this test reads nothing")
        for name, task in audits.items():
            with self.subTest(task=name):
                self.assertEqual(refusals(task), [])

    def test_a_task_that_could_write_is_refused(self) -> None:
        reading = {"run": ['uv run ruff check --select "${usage_rules}" .'], "file": None, "depends": [], "hide": True}
        self.assertEqual(refusals(reading), [])
        for script, why in (
            ('uv run ruff check "$@"', "passes its arguments through"),
            ("uv run ruff check {{arg(name='rules')}}", "passes its arguments through"),
        ):
            with self.subTest(script=script):
                self.assertIn(why, refusals(reading | {"run": [script]}))
        self.assertIn("depends on another task", refusals(reading | {"depends": ["fix"]}))
        self.assertIn("is listed by `mise tasks`", refusals(reading | {"hide": False}))


# The tasks that hand their words to a tool as paths, globs or settings, each
# with the usage variable that carries them.
_WORD_TAKERS = {"mise-tasks/audit/ruff": "usage_paths", "mise-tasks/audit/ast": "usage_globs"}


@tag("source")
class AuditWordTests(SimpleTestCase):
    """A word that could reach the tool as a flag or a setting of its own is
    refused before the tool runs: PATH holds no tool, so one that ran fails
    differently."""

    def test_a_word_that_is_not_a_path_or_glob_is_refused(self) -> None:
        for script, variable in _WORD_TAKERS.items():
            for words in ("--fix", "-U", "'fix = true'", "\"a'], fix = true, x = ['\"", "'!a!b'", "'a,b'"):
                with self.subTest(script=script, words=words):
                    run = subprocess.run(
                        ["/bin/sh", script],
                        cwd=ROOT,
                        env=os.environ | {"usage_rules": "F", variable: words, "PATH": "/usr/bin:/bin"},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertIn("is not a", run.stderr)
