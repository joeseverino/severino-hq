"""`mise run test --at <ref> --go` runs this tree's Go test files on the ref's Go code."""

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
from tests.mise_tasks import body, mise_tasks
from tools.gate import at
from tools.gate.tests.base import GateTestCase

# Stands in for gotestsum over go test: records the controller's files where it
# ran, says what argv[2] asks, writes the go test events in argv[4] where
# gotestsum keeps them, then exits with the status in argv[3].
PROBE = """
import json, os, pathlib, sys
here = pathlib.Path.cwd() / "controller"
seen = {str(p.relative_to(here)): p.read_text() for p in here.rglob("*") if p.is_file()}
pathlib.Path(sys.argv[1]).write_text(json.dumps(seen))
print(sys.argv[2])
if os.environ.get("GOTESTSUM_JSONFILE"):
    pathlib.Path(os.environ["GOTESTSUM_JSONFILE"]).write_text(sys.argv[4])
sys.exit(int(sys.argv[3]))
"""
# go test's events for a package that does not build, as Go 1.27's -json writes
# them (`go doc cmd/test2json`), and for one whose test fails.
UNBUILT = "example.com/m/host [example.com/m/host.test]"
NOT_BUILT = [
    {"ImportPath": UNBUILT, "Action": "build-output", "Output": "# example.com/m/host [example.com/m/host.test]\n"},
    {"ImportPath": UNBUILT, "Action": "build-fail"},
    {"Time": "2026-10-31T01:19:07.089601-05:00", "Action": "start", "Package": "example.com/m/host"},
    {
        "Time": "2026-10-31T01:19:07.090057-05:00",
        "Action": "fail",
        "Package": "example.com/m/host",
        "Elapsed": 0,
        "FailedBuild": UNBUILT,
    },
]
TEST_FAILED = [
    {"Time": "2026-10-31T01:19:07.59166-05:00", "Action": "start", "Package": "example.com/m/rules"},
    {"Time": "2026-10-31T01:19:07.60855-05:00", "Action": "run", "Package": "example.com/m/rules", "Test": "TestNo"},
    {
        "Time": "2026-10-31T01:19:07.608561-05:00",
        "Action": "fail",
        "Package": "example.com/m/rules",
        "Test": "TestNo",
        "Elapsed": 0,
    },
    {"Time": "2026-10-31T01:19:07.608705-05:00", "Action": "fail", "Package": "example.com/m/rules", "Elapsed": 0.017},
]
# The packages a run over many prints after the one that failed to build.
PASSED_AFTER = "\n".join(f"ok  \texample.com/m/pkg{n}\t0.01{n}s" for n in range(40))


@tag("source")
class TestAtGoTests(GateTestCase):
    """The task's Go mode on a fixture repository, whose root stands in for this checkout's."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "repo")
        self.seen = Path(folder.name, "seen.json")
        self.root.mkdir()
        self.git("init", "--quiet")
        self.write("controller/host/host.go", "old code\n")
        self.write("controller/host/host_test.go", "old test\n")
        self.write("controller/host/gone_test.go", "gone\n")
        self.write("controller/host/testdata/unit.service", "old fixture\n")
        self.write("tests/test_app.py", "old python\n")
        self.old = self.commit("old")
        self.write("controller/host/host.go", "new code\n")
        self.commit("new")
        # Uncommitted work counts: a test edited, one added, one removed, support changed.
        self.write("controller/host/host_test.go", "edited test\n")
        self.write("controller/host/rules_test.go", "new test\n")
        self.write("controller/host/testdata/unit.service", "new fixture\n")
        self.write("controller/fakes/fakestest/fake.go", "new fake\n")
        self.write("tests/test_app.py", "new python\n")
        Path(self.root, "controller/host/gone_test.go").unlink()
        for patcher in (
            patch.object(at, "ROOT", self.root),
            patch.object(at, "GO_TEST", [sys.executable, "-c", PROBE]),
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

    def run_main(self, said: str, code: str, keep: tuple[str, ...] = (), events: list | None = None) -> tuple[int, str]:
        out = io.TextIOWrapper(io.BytesIO(), write_through=True)
        recorded = "\n".join(json.dumps(event) for event in events or ())
        with patch("sys.stdout", out):
            typed = at.Typed(at=self.old, keep=list(keep), args=[str(self.seen), said, code, recorded], go=True)
            status = at.main(typed)
        return status, out.buffer.getvalue().decode()

    def test_the_refs_go_code_runs_under_this_trees_go_test_files_and_its_status_comes_back(self):
        code, out = self.run_main("--- FAIL: TestRules", "1")

        self.assertEqual(code, 1)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["host/host.go"], "old code\n")
        self.assertEqual(seen["host/host_test.go"], "edited test\n")
        self.assertEqual(seen["host/rules_test.go"], "new test\n")
        self.assertNotIn("host/gone_test.go", seen)
        self.assertEqual(seen["host/testdata/unit.service"], "new fixture\n")
        self.assertEqual(seen["fakes/fakestest/fake.go"], "new fake\n")
        verdict = out.strip().splitlines()[-1]
        self.assertIn(self.old[:12], verdict)
        self.assertIn("FAILED", verdict)
        self.assertIn("3 Go test files from this tree", verdict)
        self.assertIn("laid over the ref's own: controller/host/testdata/unit.service", verdict)
        self.assertIn("new in this tree: controller/fakes/fakestest/fake.go", verdict)
        rerun = shlex.split(verdict.rpartition("rerun: ")[2])
        self.assertEqual(rerun[:6], ["mise", "run", "test", "--at", self.old, "--go"])
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_a_python_test_file_is_not_laid_over_a_go_run(self):
        self.run_main("ok", "0")

        self.assertEqual(Path(self.root, "tests/test_app.py").read_text(), "new python\n")
        self.assertNotIn("tests/test_app.py", json.loads(self.seen.read_text()))
        present, _ = at.changed_tests(self.old, self.root, at.is_go_test_file)
        self.assertNotIn("tests/test_app.py", present)

    def test_kept_go_support_runs_as_the_ref_has_it(self):
        code, out = self.run_main("ok", "0", keep=("controller/host/testdata/unit.service",))

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.seen.read_text())["host/testdata/unit.service"], "old fixture\n")
        self.assertIn("kept at the ref: controller/host/testdata/unit.service", out.strip().splitlines()[-1])

    def test_a_kept_go_test_file_runs_as_the_ref_has_it_beside_this_trees_new_one(self):
        code, out = self.run_main("--- FAIL: TestRules", "1", keep=("controller/host/host_test.go",))

        self.assertEqual(code, 1)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["host/host_test.go"], "old test\n")
        self.assertEqual(seen["host/rules_test.go"], "new test\n")
        verdict = out.strip().splitlines()[-1]
        self.assertIn("2 Go test files from this tree", verdict)
        self.assertIn("kept at the ref: controller/host/host_test.go", verdict)
        rerun = shlex.split(verdict.rpartition("rerun: ")[2])
        self.assertEqual(
            rerun[:8], ["mise", "run", "test", "--at", self.old, "--keep", "controller/host/host_test.go", "--go"]
        )
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_a_kept_go_test_file_new_in_this_tree_is_refused(self):
        with patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_main("ok", "0", keep=("controller/host/rules_test.go",))

        self.assertEqual(code, 2)
        self.assertIn("controller/host/rules_test.go", err.getvalue())
        self.assertIn("nothing at the ref", err.getvalue())
        self.assertFalse(self.seen.exists())

    def test_a_package_that_does_not_build_at_the_ref_is_not_a_failing_test_however_much_follows_it(self):
        code, out = self.run_main(
            f"FAIL\texample.com/m/host [build failed]\n{PASSED_AFTER}", "1", events=[*NOT_BUILT, *TEST_FAILED]
        )

        self.assertEqual(code, at.UNIMPORTED)
        verdict = out.strip().splitlines()[-1]
        self.assertIn("DOES NOT BUILD at the ref", verdict)
        self.assertIn(UNBUILT, verdict)

    def test_a_package_that_does_not_build_names_the_proof_that_does(self):
        code, out = self.run_main(f"FAIL\texample.com/m/host [build failed]\n{PASSED_AFTER}", "1", events=NOT_BUILT)

        self.assertEqual(code, at.UNIMPORTED)
        verdict = out.strip().splitlines()[-1].partition("; rerun: ")[0]
        self.assertIn("proves nothing", verdict)
        self.assertIn("break the fix in this tree and run mise run test --go on those packages", verdict)

    def test_a_failing_test_that_prints_what_a_build_failure_prints_is_a_failing_test(self):
        code, out = self.run_main("--- FAIL: TestNo\nFAIL\texample.com/m/rules [build failed]", "1", events=TEST_FAILED)

        self.assertEqual(code, 1)
        self.assertNotIn("DOES NOT BUILD", out.strip().splitlines()[-1])

    def test_without_at_the_go_arguments_reach_go_test_as_typed(self):
        typed = ["./host", "-run", "TestRules"]
        with (
            patch.object(at, "run", return_value=at.Step("test", ok=False, code=1)) as run,
            patch("sys.stdout", io.StringIO()) as out,
        ):
            code = at.main(at.Typed(args=typed, go=True))

        self.assertEqual(code, 1)
        self.assertEqual(run.call_args.args[1], [*at.GO_TEST, *typed])
        self.assertIn("mise run test --go ./host -run TestRules", out.getvalue().splitlines()[-1])


@tag("source")
class TestGoModeTests(GateTestCase):
    def test_the_go_command_is_the_go_gates_up_to_its_packages(self):
        run = body(mise_tasks()["test:controller"])
        self.assertTrue(run.startswith(shlex.join(at.GO_TEST) + " "), run)

    def test_the_go_switch_is_read_as_mise_exports_it(self):
        environ = {"usage_at": "HEAD", "usage_go": "true", "usage_arguments": "./host -run TestRules"}

        self.assertEqual(
            at.Typed.from_usage(Usage.from_environment(environ)),
            at.Typed(at="HEAD", args=["./host", "-run", "TestRules"], go=True),
        )
