"""`mise run test --at <ref> tests/shell/test-<name>.sh` runs this tree's shell suite on the ref's scripts,
through the task lint:shell runs each suite with."""

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

from tools.gate import at
from tools.gate.tests.base import GateTestCase

# Stands in for mise: records the arguments it was given and, from the
# worktree the first suite named lives in, the script under test and every
# file under tests/, then exits with the status the test sets.
PROBE = """
import json, os, pathlib, sys
argv = sys.argv[1:]
tree = pathlib.Path(next(arg for arg in argv if arg.endswith(".sh"))).parents[2]
seen = {
    "argv": argv,
    "code": (tree / "tools" / "lib" / "thing.sh").read_text(),
    "files": {str(p.relative_to(tree)): p.read_text() for p in (tree / "tests").rglob("*") if p.is_file()},
}
pathlib.Path(os.environ["PROBE_SEEN"]).write_text(json.dumps(seen))
sys.exit(int(os.environ["PROBE_STATUS"]))
"""
SUITE = "tests/shell/test-thing.sh"
BASH_SUITE = "tests/shell/test-bash.sh"


@tag("source")
class TestAtShellTests(GateTestCase):
    """The task's shell mode on a fixture repository, whose root stands in for this checkout's."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "repo")
        self.seen = Path(folder.name, "seen.json")
        self.root.mkdir()
        self.git("init", "--quiet")
        self.write("tools/lib/thing.sh", "old code\n")
        self.write(SUITE, "#!/bin/sh\nold suite\n")
        self.write(BASH_SUITE, "#!/usr/bin/env bash\nbash suite\n")
        self.write("tests/shell/fakes/uv", "old fake\n")
        self.write("tests/test_app.py", "old python\n")
        self.old = self.commit("old")
        self.write("tools/lib/thing.sh", "new code\n")
        self.commit("new")
        # Uncommitted work counts: the suite edited, a fake changed, a fixture added.
        self.write(SUITE, "#!/bin/sh\nedited suite\n")
        self.write("tests/shell/fakes/uv", "new fake\n")
        self.write("tests/shell/fixtures/added.txt", "added fixture\n")
        self.write("tests/test_app.py", "new python\n")
        for patcher in (
            patch.object(at, "ROOT", self.root),
            patch.object(at, "MISE", [sys.executable, "-c", PROBE], create=True),
            # A run that is not the shell suite's exits with a status no case sets.
            patch.object(at, "UV_RUN", [sys.executable, "-c", "import sys; sys.exit(9)"]),
            patch.object(at, "TEST", []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        environ = patch.dict(os.environ, {"PROBE_SEEN": str(self.seen), "PROBE_STATUS": "0"})
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

    def run_main(self, *args: str, at_ref: str | None = None, go: bool = False) -> tuple[int, str]:
        out = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with patch("sys.stdout", out):
            code = at.main(at.Typed(at=at_ref, args=list(args), go=go))
        return code, out.buffer.getvalue().decode()

    def runs(self) -> list[str]:
        """The suites the recorded mise command hands the suites task."""
        argv = json.loads(self.seen.read_text())["argv"]
        return argv[argv.index(at.SHELL_TASK) + 1 :]

    def test_the_refs_scripts_run_under_this_trees_suite_and_its_status_comes_back(self):
        os.environ["PROBE_STATUS"] = "1"
        code, out = self.run_main(SUITE, at_ref=self.old)

        self.assertEqual(code, 1)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["code"], "old code\n")
        self.assertEqual(seen["files"][SUITE], "#!/bin/sh\nedited suite\n")
        self.assertEqual(seen["files"]["tests/shell/fakes/uv"], "new fake\n")
        self.assertEqual(seen["files"]["tests/shell/fixtures/added.txt"], "added fixture\n")
        self.assertEqual(seen["files"]["tests/test_app.py"], "old python\n")
        # The task comes from this checkout, so the gate's tools and environment run the ref's suite.
        self.assertEqual(seen["argv"][seen["argv"].index("-C") + 1], str(self.root))
        verdict = out.strip().splitlines()[-1]
        self.assertIn(self.old[:12], verdict)
        self.assertIn("FAILED", verdict)
        self.assertIn("1 shell suite from this tree", verdict)
        self.assertIn("laid over the ref's own: tests/shell/fakes/uv", verdict)
        self.assertIn("new in this tree: tests/shell/fixtures/added.txt", verdict)
        rerun = shlex.split(verdict.rpartition("rerun: ")[2])
        self.assertEqual(rerun, ["mise", "run", "test", "--at", self.old, SUITE])
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_each_suite_named_is_handed_to_the_task_lint_shell_runs_them_with(self):
        code, _ = self.run_main(SUITE, BASH_SUITE, at_ref=self.old)

        self.assertEqual(code, 0)
        runs = [Path(suite).relative_to(Path(suite).parents[2]).as_posix() for suite in self.runs()]
        self.assertEqual(runs, [SUITE, BASH_SUITE])

    def test_without_at_the_suite_runs_here_through_the_same_task(self):
        os.environ["PROBE_STATUS"] = "1"
        code, out = self.run_main(SUITE)

        self.assertEqual(code, 1)
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["code"], "new code\n")
        self.assertEqual(self.runs(), [str(self.root / SUITE)])
        self.assertIn(f"mise run test {SUITE}", out.strip().splitlines()[-1])

    def test_a_shell_suite_beside_anything_else_is_refused(self):
        for args, go in (([SUITE, "tests.test_app"], False), ([SUITE, "-k", "x"], False), ([SUITE], True)):
            with self.subTest(args=args, go=go), patch("sys.stderr", io.StringIO()) as err:
                code, _ = self.run_main(*args, at_ref=self.old, go=go)
                self.assertEqual(code, 2)
                self.assertIn(at.SHELL_TASK, err.getvalue())
                self.assertFalse(self.seen.exists())


# A fixture suite: it logs the shell it was run under, then exits with the status its name ends in.
FIXTURE = 'echo "{name} ${{SHELL_UNDER_TEST:-as its first line says}}" >>"$SUITES_LOG"\nexit {status}\n'


@tag("source")
class TestShellModeTests(GateTestCase):
    def suites(self, folder: str, suites: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """Runs the task lint:shell runs the suites with, on fixture suites each a first line and a status."""
        log = Path(folder, "log")
        paths = []
        for name, first in suites.items():
            path = Path(folder, f"test-{name}.sh")
            path.write_text(f"{first}\n" + FIXTURE.format(name=name, status=name.rpartition("-")[2]))
            path.chmod(0o755)
            paths.append(str(path))
        result = subprocess.run(
            [*at.MISE, "-C", str(at.ROOT), "run", at.SHELL_TASK, *paths],
            env={**os.environ, "SUITES_LOG": str(log)},
            capture_output=True,
            text=True,
            check=False,
        )
        return result, sorted(log.read_text().splitlines()) if log.exists() else []

    def test_a_sh_suite_runs_under_each_shell_sh_is_and_any_other_once_as_its_first_line_says(self):
        shells = ["/bin/sh", *(["/bin/dash"] if os.access("/bin/dash", os.X_OK) else [])]
        with tempfile.TemporaryDirectory() as folder:
            result, ran = self.suites(folder, {"posix-0": "#!/bin/sh", "bash-0": "#!/usr/bin/env bash"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ran, sorted(["bash-0 as its first line says", *(f"posix-0 {shell}" for shell in shells)]))

    def test_every_suite_runs_though_one_fails_and_the_run_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            result, ran = self.suites(folder, {"failing-1": "#!/usr/bin/env bash", "passing-0": "#!/usr/bin/env bash"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(ran, ["failing-1 as its first line says", "passing-0 as its first line says"])

    def test_a_suite_is_told_apart_by_its_path(self):
        self.assertTrue(at.is_shell_suite("tests/shell/test-heavy.sh"))
        self.assertTrue(at.is_shell_suite("./tests/shell/test-heavy.sh"))
        for path in ("tests/shell/fakes/test-uv.sh", "tests/test_app.py", "hq.core.tests", "-k"):
            with self.subTest(path=path):
                self.assertFalse(at.is_shell_suite(path))
