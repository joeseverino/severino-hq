"""`mise run test --at <ref>` runs this tree's tests on the ref's code, and leaves nothing behind."""

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag

from tools.gate import at
from tools.gate.tests.base import GateTestCase

# Stands in for manage.py test: records what it ran on, then exits with the status it is given.
PROBE = """
import json, pathlib, sys
here = pathlib.Path.cwd()
seen = {
    "code": (here / "app.py").read_text(),
    "tests": sorted(str(p.relative_to(here)) for p in here.rglob("*.py") if p.name != "app.py"),
    "changed": (here / "tests" / "test_app.py").read_text(),
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(seen))
sys.exit(int(sys.argv[2]))
"""


@tag("source")
class TestAtRefTests(GateTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "repo")
        self.seen = Path(folder.name, "seen.json")
        self.root.mkdir()
        self.git("init", "--quiet")
        self.write("app.py", "VALUE = 1\n")
        self.write("tests/test_app.py", "old\n")
        self.write("tests/test_gone.py", "gone\n")
        self.write("hq/core/estate_browser_tests.py", "old browser\n")
        self.old = self.commit("old")
        self.write("app.py", "VALUE = 2\n")
        self.write("hq/core/estate_browser_tests.py", "new browser\n")
        self.commit("new")
        # Uncommitted work counts: a test being written, one edited, one removed.
        self.write("tests/test_app.py", "edited\n")
        self.write("tests/test_new.py", "new\n")
        Path(self.root, "tests/test_gone.py").unlink()
        for name, value in {"ROOT": self.root, "UV_RUN": [sys.executable, "-c", PROBE], "TEST": []}.items():
            patcher = patch.object(at, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)

    def git(self, *args: str) -> str:
        identity = ["-c", "user.name=Example", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]
        return subprocess.run(
            ["git", *identity, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, path: str, text: str) -> None:
        Path(self.root, path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.root, path).write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "--quiet", "--no-verify", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_main(self, *argv: str) -> tuple[int, str]:
        out = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with patch("sys.stdout", out):
            code = at.main(list(argv))
        return code, out.buffer.getvalue().decode()

    def assert_nothing_left(self):
        self.assertEqual(list(Path(self.root, "var", "test-at").glob("*")), [])
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_the_refs_code_runs_under_this_trees_tests_and_its_status_comes_back(self):
        code, out = self.run_main("--at", self.old, str(self.seen), "3")

        self.assertEqual(code, 3)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["code"], "VALUE = 1\n")
        self.assertEqual(seen["changed"], "edited\n")
        self.assertIn("tests/test_new.py", seen["tests"])
        self.assertNotIn("tests/test_gone.py", seen["tests"])
        self.assertEqual(Path(self.root, "hq/core/estate_browser_tests.py").read_text(), "new browser\n")
        verdict = out.strip().splitlines()[-1]
        self.assertIn(self.old[:12], verdict)
        self.assertIn("FAILED", verdict)
        self.assert_nothing_left()

    def test_a_pass_at_the_ref_names_it(self):
        code, out = self.run_main(f"--at={self.old}", str(self.seen), "0")

        self.assertEqual(code, 0)
        self.assertIn(f"at {self.old} ", out.strip().splitlines()[-1])
        self.assert_nothing_left()

    def run_unittest_at_old(self, test: str) -> tuple[int, str]:
        """The label run by the real test loader, so its report reads as a real one does."""
        self.write("tests/test_app.py", test)
        with patch.object(at, "UV_RUN", [sys.executable, "-m", "unittest"]):
            return self.run_main("--at", self.old, "tests.test_app")

    def test_a_label_that_does_not_import_at_the_ref_is_not_a_failing_test(self):
        code, out = self.run_unittest_at_old(
            "import unittest\nfrom app import RENAMED\n\n"
            "class Case(unittest.TestCase):\n    def test_renamed(self):\n        self.assertTrue(RENAMED)\n"
        )

        self.assertEqual(code, 3)
        verdict = out.strip().splitlines()[-1]
        self.assertIn("DOES NOT IMPORT", verdict)
        self.assertIn(self.old, verdict)
        self.assertIn("test_app", verdict)
        kept = Path(out.strip().splitlines()[-2].rsplit(" ", 1)[-1]).read_text()
        self.assertIn("cannot import name 'RENAMED'", kept)
        self.assert_nothing_left()

    def test_a_test_that_imports_and_fails_at_the_ref_is_a_failure(self):
        code, out = self.run_unittest_at_old(
            "import unittest\nfrom app import VALUE\n\n"
            "class Case(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(VALUE, 2)\n"
        )

        self.assertEqual(code, 1)
        verdict = out.strip().splitlines()[-1]
        self.assertIn("FAILED", verdict)
        self.assertNotIn("DOES NOT IMPORT", verdict)

    def test_an_interrupted_run_leaves_no_worktree(self):
        with patch.object(at, "run", side_effect=KeyboardInterrupt):
            code, _ = self.run_main("--at", self.old, "label")

        self.assertEqual(code, 130)
        self.assert_nothing_left()

    def test_a_ref_that_names_no_commit_is_refused(self):
        with patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_main("--at", "no-such-ref", "label")

        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", err.getvalue())
        self.assert_nothing_left()

    def test_at_needs_a_ref(self):
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_main("--at")[0], 2)


@tag("source")
class TestHereTests(GateTestCase):
    def test_without_at_what_follows_the_labels_reaches_manage_py_test_as_typed(self):
        typed = ["hq.example.tests", "--parallel", "1", "-k", "example"]
        with patch.object(at.os, "execv", side_effect=SystemExit) as execv, self.assertRaises(SystemExit):
            at.main(typed)

        program, argv = execv.call_args.args
        self.assertEqual(program, sys.executable)
        self.assertEqual(argv[1:3], ["manage.py", "test"])
        self.assertEqual(argv[-len(typed) :], typed)
