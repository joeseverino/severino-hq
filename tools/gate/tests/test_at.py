"""`mise run test --at <ref>` runs this tree's tests on the ref's code, and leaves nothing behind."""

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag

from hq.platform.core.tests.task import Usage
from tools.gate import at
from tools.gate.tests.base import UNITTEST, GateTestCase

CHECKOUT = Path(__file__).resolve().parents[3]
# The test command as the task runs it, before setUp replaces it.
MANAGE_PY_TEST = at.TEST

# Stands in for manage.py test: records what it ran on, then exits with the status it is given.
PROBE = """
import json, pathlib, sys
here = pathlib.Path.cwd()
seen = {
    "code": (here / "app.py").read_text(),
    "tests": sorted(str(p.relative_to(here)) for p in here.rglob("*.py") if p.name != "app.py"),
    "changed": (here / "tests" / "test_app.py").read_text(),
    "support": (here / "hq" / "core" / "tests" / "browser" / "estate.py").read_text(),
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(seen))
sys.exit(int(sys.argv[2]))
"""
# Stands in for manage.py test over support that holds the fix: appends the support each run saw, then
# exits with argv[2] under this tree's support and argv[3] under the ref's.
SUPPORT_PROBE = """
import json, pathlib, sys
support = (pathlib.Path.cwd() / "hq" / "core" / "tests" / "browser" / "estate.py").read_text()
seen = pathlib.Path(sys.argv[1])
runs = json.loads(seen.read_text()) if seen.exists() else []
seen.write_text(json.dumps([*runs, support]))
sys.exit(int(sys.argv[3] if support == "old browser\\n" else sys.argv[2]))
"""


@tag("source")
class TestAtRefTests(GateTestCase):
    """The task run on a fixture repository, whose root stands in for this checkout's."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "repo")
        self.seen = Path(folder.name, "seen.json")
        self.root.mkdir()
        self.git("init", "--quiet")
        self.write("app.py", "VALUE = 1\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_app.py", "old\n")
        self.write("tests/test_gone.py", "gone\n")
        self.write("hq/core/tests/browser/estate.py", "old browser\n")
        self.old = self.commit("old")
        self.write("app.py", "VALUE = 2\n")
        self.write("hq/core/tests/browser/estate.py", "new browser\n")
        self.commit("new")
        # Uncommitted work counts: a test being written, one edited, one removed.
        self.write("tests/test_app.py", "edited\n")
        self.write("tests/test_new.py", "new\n")
        Path(self.root, "tests/test_gone.py").unlink()
        for patcher in (
            patch.object(at, "ROOT", self.root),
            patch.object(at, "UV_RUN", [sys.executable, "-c", PROBE]),
            patch.object(at, "TEST", []),
        ):
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

    def run_main(self, *args: str, at_ref: str, keep: tuple[str, ...] = ()) -> tuple[int, str]:
        out = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with patch("sys.stdout", out):
            code = at.main(at.Typed(at=at_ref, keep=list(keep), args=list(args)))
        return code, out.buffer.getvalue().decode()

    def assert_nothing_left(self):
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_the_refs_code_runs_under_this_trees_tests_and_its_status_comes_back(self):
        code, out = self.run_main(str(self.seen), "3", at_ref=self.old)

        self.assertEqual(code, 3)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["code"], "VALUE = 1\n")
        self.assertEqual(seen["changed"], "edited\n")
        self.assertIn("tests/test_new.py", seen["tests"])
        self.assertNotIn("tests/test_gone.py", seen["tests"])
        self.assertEqual(Path(self.root, "hq/core/tests/browser/estate.py").read_text(), "new browser\n")
        verdict = out.strip().splitlines()[-1]
        self.assertIn(self.old[:12], verdict)
        self.assertIn("FAILED", verdict)
        self.assert_nothing_left()

    def test_support_is_laid_over_and_named_and_test_modules_are_counted(self):
        code, out = self.run_main(str(self.seen), "1", at_ref=self.old)

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(self.seen.read_text())["support"], "new browser\n")
        verdict = out.strip().splitlines()[-1]
        self.assertIn("3 test modules from this tree", verdict)
        self.assertIn("hq/core/tests/browser/estate.py", verdict)
        self.assertNotIn("kept at the ref", verdict)

    def test_support_that_replaced_the_refs_copy_is_named_apart_from_support_the_ref_lacks(self):
        """A run that laid this tree's code over the ref's tested this tree's, and the line says which files did."""
        self.write("tests/helpers.py", "new helper\n")
        code, out = self.run_main(str(self.seen), "1", at_ref=self.old)

        self.assertEqual(code, 1)
        verdict = out.strip().splitlines()[-1]
        replaced = verdict.partition("laid over the ref's own: ")[2]
        self.assertTrue(replaced.startswith("hq/core/tests/browser/estate.py"), verdict)
        self.assertIn("--keep", replaced)
        added = verdict.partition("new in this tree: ")[2]
        self.assertTrue(added.startswith("tests/helpers.py"), verdict)
        self.assertNotIn("tests/helpers.py", replaced.partition(";")[0])
        self.assert_nothing_left()

    def test_support_whose_name_git_quotes_is_found_at_the_ref(self):
        name = "tests/fakes/unit\\x2dname.txt"
        self.write(name, "old fake\n")
        ref = self.commit("a fake whose name holds a backslash")

        self.assertEqual(at.new_at(ref, [name, "tests/test_absent.py"]), ["tests/test_absent.py"])

    def test_kept_support_runs_as_the_ref_has_it_and_the_line_says_so(self):
        code, out = self.run_main(str(self.seen), "0", at_ref=self.old, keep=("hq/core/tests/browser/estate.py",))

        self.assertEqual(code, 0)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["support"], "old browser\n")
        self.assertEqual(seen["changed"], "edited\n")
        verdict = out.strip().splitlines()[-1]
        self.assertIn("kept at the ref: hq/core/tests/browser/estate.py", verdict)
        self.assertNotIn("laid over the ref's own", verdict)
        self.assert_nothing_left()

    def test_a_module_a_label_names_is_a_test_module_whatever_its_name(self):
        label = "hq.core.tests.browser.estate.EstateTests"
        code, out = self.run_main(str(self.seen), "0", label, at_ref=self.old)

        self.assertEqual(code, 0)
        self.assertIn("4 test modules from this tree", out.strip().splitlines()[-1])
        self.assert_nothing_left()

    def test_a_kept_test_module_runs_as_the_ref_has_it_beside_this_trees_new_one(self):
        code, out = self.run_main(str(self.seen), "1", at_ref=self.old, keep=("tests/test_app.py",))

        self.assertEqual(code, 1)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["changed"], "old\n")
        self.assertIn("tests/test_new.py", seen["tests"])
        verdict = out.strip().splitlines()[-1]
        self.assertIn("2 test modules from this tree", verdict)
        self.assertIn("kept at the ref: tests/test_app.py", verdict)
        rerun = shlex.split(verdict.rpartition("rerun: ")[2])
        self.assertEqual(rerun[:7], ["mise", "run", "test", "--at", self.old, "--keep", "tests/test_app.py"])
        self.assert_nothing_left()

    def test_a_kept_test_module_new_in_this_tree_is_refused(self):
        with patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_main(str(self.seen), "0", at_ref=self.old, keep=("tests/test_new.py",))

        self.assertEqual(code, 2)
        self.assertIn("tests/test_new.py", err.getvalue())
        self.assertIn("nothing at the ref", err.getvalue())
        self.assertFalse(self.seen.exists())
        self.assert_nothing_left()

    def test_a_keep_that_names_no_changed_support_is_refused(self):
        for keep in ("app.py", "hq/core/tests/browser/missing.py"):
            with self.subTest(keep=keep), patch("sys.stderr", io.StringIO()) as err:
                code, _ = self.run_main(str(self.seen), "0", at_ref=self.old, keep=(keep,))
                self.assertEqual(code, 2)
                self.assertIn(keep, err.getvalue())
                self.assertFalse(self.seen.exists())
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(self.run_main("label", at_ref=self.old, keep=("",))[0], 2)
        self.assertIn("--keep", err.getvalue().splitlines()[-1])
        self.assert_nothing_left()

    def test_a_pass_at_the_ref_names_it(self):
        code, out = self.run_main(str(self.seen), "0", at_ref=self.old)

        self.assertEqual(code, 0)
        self.assertIn(f"at {self.old} ", out.strip().splitlines()[-1])
        self.assert_nothing_left()

    def run_support_probe(self, here: str, at_ref: str, keep: tuple[str, ...] = ()) -> tuple[int, str, list[str]]:
        """A run whose test passes or fails by the support it finds: ``here`` is the status under this
        tree's, ``at_ref`` under the ref's; with the support each run saw, in order."""
        with patch.object(at, "UV_RUN", [sys.executable, "-c", SUPPORT_PROBE]):
            code, out = self.run_main(str(self.seen), here, at_ref, at_ref=self.old, keep=keep)
        return code, out.strip().splitlines()[-1], json.loads(self.seen.read_text())

    def test_a_pass_with_support_laid_over_runs_again_with_it_kept_at_the_ref_and_that_run_is_the_verdict(self):
        """The fix in a support module passes under this tree's copy, so the run that shows the test fail
        without the fix is the one with the ref's copy."""
        code, verdict, runs = self.run_support_probe("0", "1")

        self.assertEqual(code, 1)
        self.assertEqual(runs, ["new browser\n", "old browser\n"])
        self.assertIn("FAILED", verdict)
        self.assertIn("kept at the ref: hq/core/tests/browser/estate.py", verdict)
        self.assertIn("this tree's hq/core/tests/browser/estate.py laid over instead, it passed", verdict)
        rerun = shlex.split(verdict.rpartition("rerun: ")[2])
        self.assertEqual(
            rerun[:7], ["mise", "run", "test", "--at", self.old, "--keep", "hq/core/tests/browser/estate.py"]
        )
        self.assert_nothing_left()

    def test_a_pass_under_either_support_is_a_pass_that_says_both(self):
        code, verdict, runs = self.run_support_probe("0", "0")

        self.assertEqual(code, 0)
        self.assertEqual(runs, ["new browser\n", "old browser\n"])
        self.assertIn("passed", verdict)
        self.assertIn("kept at the ref: hq/core/tests/browser/estate.py", verdict)
        self.assertIn("this tree's hq/core/tests/browser/estate.py laid over instead", verdict)
        self.assert_nothing_left()

    def test_a_failure_or_a_run_that_laid_no_support_over_runs_once(self):
        for here, keep in (("1", ()), ("0", ("hq/core/tests/browser/estate.py",))):
            with self.subTest(here=here, keep=keep):
                self.seen.unlink(missing_ok=True)
                code, verdict, runs = self.run_support_probe(here, "1", keep=keep)

                self.assertEqual(code, 1)
                self.assertEqual(len(runs), 1)
                self.assertNotIn("laid over instead", verdict)
        self.assert_nothing_left()

    def test_support_the_new_test_needs_kept_at_the_ref_is_no_proof_and_the_line_says_so(self):
        """Kept at the ref, a fixture the new test imports is missing, so that run reached no test."""
        self.write("tests/fixture.py", "")
        self.write(
            "tests/test_app.py",
            "import unittest\nfrom tests.fixture import NEEDED\n\n"
            "class Case(unittest.TestCase):\n    def test_needed(self):\n        self.assertTrue(NEEDED)\n",
        )
        ref = self.commit("a fixture without what the new test needs")
        self.write("tests/fixture.py", "NEEDED = True\n")
        self.write("tests/test_app.py", Path(self.root, "tests/test_app.py").read_text() + "\n")
        os.environ["PYTHONPATH"] = str(CHECKOUT)
        with patch.object(at, "UV_RUN", [sys.executable, "-c", UNITTEST]):
            code, out = self.run_main("tests.test_app", at_ref=ref)

        self.assertEqual(code, at.UNIMPORTED)
        verdict = out.strip().splitlines()[-1]
        self.assertIn("DOES NOT IMPORT", verdict)
        self.assertIn("kept at the ref: tests/fixture.py", verdict)
        self.assertIn("this tree's tests/fixture.py laid over instead, it passed", verdict)
        self.assertIn("fixture the new test needs", verdict)
        self.assert_nothing_left()

    def run_unittest_at_old(self, test: str) -> tuple[int, str]:
        """The label run by the real test loader, so its failures read as a real run's do."""
        self.write("tests/test_app.py", test)
        # A package, so `tests` is this one rather than the checkout's the runner is imported from.
        self.write("tests/__init__.py", "")
        os.environ["PYTHONPATH"] = str(CHECKOUT)
        with patch.object(at, "UV_RUN", [sys.executable, "-c", UNITTEST]):
            return self.run_main("tests.test_app", at_ref=self.old)

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

    def test_a_label_that_does_not_import_names_the_proof_that_does(self):
        """No test ran on the ref's code, so the line names the run in this tree that proves the test."""
        code, out = self.run_unittest_at_old("import unittest\nfrom app import RENAMED\n")

        self.assertEqual(code, at.UNIMPORTED)
        verdict = out.strip().splitlines()[-1].partition("; rerun: ")[0]
        self.assertIn("proves nothing", verdict)
        self.assertIn("break the fix in this tree and run mise run test tests.test_app", verdict)
        self.assert_nothing_left()

    def test_a_label_in_a_package_the_ref_lacks_names_the_oldest_ref_it_runs_at(self):
        """A package a later commit adds names a layout the ref does not have,
        and a runner too old to write a failure record still reads as one that
        does not import."""
        self.write("app.py", "VALUE = 2\nMOVED = 1\n")
        self.write("tests/moved/__init__.py", "")
        self.write("tests/moved/test_case.py", "import unittest\nfrom app import MOVED\n")
        split = self.commit("split")
        with patch.object(at, "UV_RUN", [sys.executable, "-m", "unittest"]):
            code, out = self.run_main("tests.moved.test_case", at_ref=self.old)

        self.assertEqual(code, at.UNIMPORTED)
        verdict = out.strip().splitlines()[-1]
        self.assertIn("DOES NOT IMPORT", verdict)
        self.assertIn(
            f"tests.moved is not at the ref; the oldest ref it runs at is {self.git('rev-parse', '--short', split)}",
            verdict,
        )
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
            code, _ = self.run_main("label", at_ref=self.old)

        self.assertEqual(code, 130)
        self.assert_nothing_left()

    def test_without_at_what_follows_the_labels_reaches_manage_py_test_as_typed(self):
        typed = ["hq.example.tests", "--parallel", "1", "-k", "example"]
        with (
            patch.object(at, "TEST", MANAGE_PY_TEST),
            patch.object(at, "run", return_value=at.Step("test", ok=False, code=1)) as run,
            patch("sys.stdout", io.StringIO()) as out,
        ):
            code = at.main(at.Typed(args=typed))

        self.assertEqual(code, 1)
        argv = run.call_args.args[1]
        self.assertEqual(argv[:3], [sys.executable, "manage.py", "test"])
        self.assertEqual(argv[-len(typed) :], typed)
        self.assertTrue(out.getvalue().splitlines()[-1].startswith("[test] FAILED"))

    def test_a_ref_that_names_no_commit_is_refused(self):
        with patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_main("label", at_ref="no-such-ref")

        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", err.getvalue())
        self.assert_nothing_left()

    def test_an_empty_ref_is_refused(self):
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_main("label", at_ref="")[0], 2)


@tag("source")
class TestTypedTests(GateTestCase):
    def test_the_arguments_are_read_as_mise_exports_them(self):
        # As mise exports `--at HEAD --keep 'x y' --keep z a.b -k "it's"`.
        environ = {"usage_at": "HEAD", "usage_keep": "'x y' z", "usage_arguments": "a.b -k 'it'\\''s'"}

        self.assertEqual(
            at.Typed.from_usage(Usage.from_environment(environ)),
            at.Typed(at="HEAD", keep=["x y", "z"], args=["a.b", "-k", "it's"]),
        )

    def test_without_at_there_is_no_ref(self):
        self.assertIsNone(at.Typed.from_usage(Usage.from_environment({"usage_arguments": "a.b"})).at)
        self.assertEqual(at.Typed.from_usage(Usage.from_environment({"usage_at": ""})).at, "")
