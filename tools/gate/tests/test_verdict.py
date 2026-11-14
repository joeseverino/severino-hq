"""A gate's closing line names what failed and ends on the command that reruns
just that, a gate run under another leaves its failed tests to the outer
verdict too, and every gate's first line names the file its verdict is kept in.

A failed command's output read back through a tool that keeps only a head and
tail of its first part never shows the last line, so the head says where the
verdict is.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag

from hq.platform.core.tests.task import Usage
from tools.gate import at, check, verdict as gate_verdict
from tools.gate.tests.base import UNITTEST, GateTestCase
from tools.gate.tests.fast_runs import Recorder, run_main
from tools.gate.tests.test_at import PROBE, TestAtRefTests
from tools.gate.tests.test_fast import RUNNER_FAILURES, VERDICT_IN

ROOT = Path(__file__).resolve().parents[3]
# Past the read-back window of a failed command's output, so its last line is cut.
LONG = "for n in range(3000): print(f'line {n} ' + '0' * 60)\nraise SystemExit(1)"

# A command that prints the lines it is given, as mise and the runner print
# theirs, records the tests it is given as the runner records its failures,
# and fails.
SAYS = """
import json, os, sys
from pathlib import Path
from unittest.mock import patch
lines, *tests = sys.argv[1:]
if tests:
    records = [{"test": test, "exception": "", "traceback": ""} for test in tests]
    Path(os.environ["HQ_TEST_REPORTS"], "1.json").write_text(json.dumps(records))
for line in json.loads(lines):
    print(line)
sys.exit(1)
"""


@tag("source")
class NestedVerdictTests(GateTestCase):
    def test_the_outer_verdict_names_the_tests_an_inner_gate_saw_fail(self):
        """As `check` runs `test --composed`, which keeps a verdict of its own."""

        kept = str(gate_verdict.VERDICTS)
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "test_example.py").write_text(RUNNER_FAILURES)
            unittest = [sys.executable, "-c", UNITTEST, "discover", "-s", folder, "-t", folder]
            inner = [sys.executable, "-c", VERDICT_IN, kept, "inner", *unittest]
            result = subprocess.run(
                [sys.executable, "-c", VERDICT_IN, kept, "outer", *inner],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout)
        # The inner gate printed into the outer one's log, which keeps the outer verdict too.
        printed = Path(kept, "outer.log").read_text().splitlines()
        inner_line, outer_line = (line for line in printed if " FAILED in " in line)
        for line in (inner_line, outer_line):
            with self.subTest(line=line):
                self.assertIn("tests: test_example.Case.test_compares, test_example.Case.test_raises", line)
        outer = Path(kept, "outer.txt").read_text().splitlines()
        self.assertIn("test_example.Case.test_raises: LookupError: no example.invalid record", outer)


@tag("source")
class WaitedForTheTurnTests(GateTestCase):
    def test_the_closing_line_splits_the_wait_from_the_total_and_a_gate_that_never_waited_says_nothing(self):
        # As mise 2026.11 prints a wait for a task's lock, under a task and under the turn a task asked for.
        held = "held by lint:source in ~/example/worktree (pid 4121, for 1m02s)"
        waited = gate_verdict.Step("waits")
        for line in (
            f"[lint:shell] waiting for lock heavy, {held}",
            "[lint:shell] got lock heavy after 6m40s",
            f"[test:composed] [turn] waiting for lock heavy, {held}",
            "[test:composed] [turn] got lock heavy after 9.6s",
        ):
            waited.read(line)
        quiet = gate_verdict.Step("quiet")
        quiet.read(f"[lint:shell] waiting for lock heavy, {held}")
        started = gate_verdict.time.monotonic()
        self.assertRegex(
            gate_verdict.verdict("g", started, [quiet, waited]), r"passed in [\d.]+s, 410s of it waiting for the turn"
        )
        self.assertNotIn("waiting", gate_verdict.verdict("g", started, [quiet]))

    def test_a_run_in_the_turn_fails_as_the_task_that_asked_for_it(self):
        # As mise 2026.11 prints a failed command a file task ran in the turn, under
        # an aggregate: the turn's line under the task, then the task's own.
        step = gate_verdict.Step("check")
        for line in ("[weighs] [turn] ERROR task failed", "Finished in 23.7ms", "[weighs] ERROR task failed"):
            step.read(line)
        self.assertEqual(step.tasks, ["weighs"])
        # The turn's line alone still names the task it ran for.
        step = gate_verdict.Step("check")
        step.read("[test:composed] [turn] ERROR task failed")
        self.assertEqual(step.tasks, ["test:composed"])
        # A step that runs the turn itself (fast's tests) is named by the step.
        step = gate_verdict.Step("tests")
        step.read("[turn] ERROR task failed")
        self.assertEqual(step.tasks, [])


@tag("source")
class RerunTests(GateTestCase):
    def gate(self, name: str, *cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), name, *cmd],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def closing(self, name: str, lines: list[str], *tests: str) -> str:
        """The closing line of a gate whose command printed ``lines`` and failed ``tests``."""

        result = self.gate(name, sys.executable, "-c", SAYS, json.dumps(lines), *tests)
        self.assertEqual(result.returncode, 1, result.stdout)
        return result.stdout.splitlines()[-1]

    def test_an_aggregate_is_named_by_the_leaf_it_failed_under_alone(self):
        line = self.closing(
            "check",
            [
                "[lint] [lint:mise-format] exited with status 1",
                "[browser] [browser:webkit] exited with status 1",
                "[lint] exited with status 1",
                "[browser] exited with status 1",
            ],
        )
        failed = line.split(": ", 1)[1].split("; rerun: ")[0].split("; ")
        self.assertEqual(failed, ["lint:mise-format", "browser:webkit"])
        self.assertTrue(line.endswith("; rerun: mise run -c lint:mise-format ::: browser:webkit"), line)

    def test_a_line_a_tool_quotes_from_a_file_names_no_task(self):
        """As a formatter's diff of a test that holds mise's lines."""

        line = self.closing(
            "fast",
            [
                '[lint:ruff]     -     ["[lint:ruff] exited with status 1", "[lint:source] exited with status 1"],',
                '[lint:ruff] 101 +    "[lint:source] FAIL: test_x (example.tests.Case.test_x)",',
                "[lint:ruff] exited with status 1",
            ],
        )
        self.assertTrue(line.endswith(": lint:ruff; rerun: mise run lint:ruff"), line)

    def test_a_task_that_failed_alone_is_named_and_rerun(self):
        line = self.closing("test", ["[test:controller] ERROR task failed"])
        self.assertTrue(line.endswith(": test:controller; rerun: mise run test:controller"), line)

    def test_failed_tests_rerun_by_label_rather_than_the_task_that_ran_them(self):
        test = "example.tests.test_a.Case.test_x"
        line = self.closing(
            "lint",
            [
                "[lint:ruff] exited with status 1",
                f"[lint:source] FAIL: test_x ({test})",
                "[lint:source] exited with status 1",
            ],
            test,
        )
        self.assertTrue(line.endswith(f"; rerun: mise run -c lint:ruff ::: test {test}"), line)

    def test_a_class_fixture_and_a_module_that_did_not_import_rerun_by_what_they_name(self):
        line = self.closing(
            "test", [], "setUpClass (example.tests.test_a.Case)", "unittest.loader._FailedTest.example.tests.test_b"
        )
        self.assertTrue(line.endswith("; rerun: mise run test example.tests.test_a.Case example.tests.test_b"), line)

    def test_a_test_of_the_composed_suite_reruns_composed(self):
        test = "example.tests.Case.test_x"
        for name, lines in (
            ("check", [f"[test:composed] FAIL: test_x ({test})", "[test:composed] exited with status 1"]),
            ("test:composed", []),
        ):
            with self.subTest(name=name):
                line = self.closing(name, lines, test)
                self.assertTrue(line.endswith(f"; rerun: mise run test --composed {test}"), line)

    def test_many_failed_tests_rerun_by_their_classes(self):
        tests = [f"example.tests.Case{n % 2}.test_{n}" for n in range(gate_verdict.SHOWN + 2)]
        line = self.closing("test", [], *tests)
        self.assertTrue(line.endswith("; rerun: mise run test example.tests.Case0 example.tests.Case1"), line)

    def test_a_step_that_names_nothing_reruns_by_its_own_command(self):
        result = self.gate("check", "--step", "deploy host", sys.executable, "-c", "raise SystemExit(3)")
        line = result.stdout.splitlines()[-1]
        self.assertTrue(line.endswith(": deploy host; rerun: uv run python -c 'raise SystemExit(3)'"), line)

    def test_each_failed_leaf_s_last_lines_are_kept_and_shown_above_the_verdict(self):
        """A failed test's traceback says why it failed, so its task's lines are not kept."""

        test = "example.tests.test_a.Case.test_x"
        said = [f"[lint] [lint:shell] example.sh: finding {n}" for n in range(gate_verdict.TAIL_KEPT + 5)]
        lines = [
            *said,
            "[lint] [lint:ruff] All checks passed!",
            f"[lint] [lint:source] FAIL: test_x ({test})",
            "[lint] [lint:shell] exited with status 1",
            "[lint] [lint:source] exited with status 1",
            "[lint] exited with status 1",
        ]
        result = self.gate("tails", sys.executable, "-c", SAYS, json.dumps(lines), test)
        printed = result.stdout.splitlines()
        self.assertTrue(printed[-1].endswith(f"; rerun: mise run -c lint:shell ::: test {test}"), printed[-1])
        shown = [f"example.sh: finding {n}" for n in range(len(said) - gate_verdict.TAIL_KEPT, len(said))]
        heading = printed.index("[tails] lint:shell ended on:")
        self.assertEqual([line.split("   ", 1)[1] for line in printed[heading + 1 : -2]], shown)
        kept = Path(gate_verdict.VERDICTS, "tails.txt").read_text().splitlines()
        at = kept.index("lint:shell ended on:")
        tail = [f"example.sh: finding {n}" for n in range(len(said) - gate_verdict.TAIL_KEPT, len(said))]
        self.assertEqual(kept[at + 1 : at + 1 + gate_verdict.TAIL_KEPT], tail)
        self.assertEqual([line for line in kept if line.endswith(" ended on:")], ["lint:shell ended on:"])

    def test_a_pass_names_no_rerun_and_the_run_before_is_kept_beside_it(self):
        self.closing("kept", ["[lint:ruff] exited with status 1"])
        passed = self.gate("kept", sys.executable, "-c", "pass")
        self.assertNotIn("rerun", passed.stdout)
        latest = Path(gate_verdict.VERDICTS, "kept.txt").read_text()
        before = Path(gate_verdict.VERDICTS, "kept.prev.txt").read_text()
        self.assertTrue(latest.splitlines()[1].startswith("[kept] passed in"), latest)
        self.assertTrue(before.splitlines()[1].endswith("; rerun: mise run lint:ruff"), before)

    def test_what_a_run_says_it_spent_is_kept_in_the_verdict_file(self):
        classes = "Slowest test classes (load average at start / end: 1.00 1.00 1.00 / 2.00 2.00 2.00):"
        measured = "heavy: 9.0s wall, 20.0s CPU, load average 2.00 2.00 2.00, peak memory 600 MB: gate 40 MB"
        said = [
            f"[fast:tests] {classes}",
            "[fast:tests]      284.1s  hq.example.tests.test_pages.EveryPageTests",
            "[fast:tests]        3.2s  hq.example.tests.test_rows.RowTests",
            "[fast:tests] Finished in 290.0s",
            "   12.0s  not a class time, said after the heading's block ended",
            f"[test] {measured}",
        ]
        passed = self.gate("spent", sys.executable, "-c", f"print({json.dumps(chr(10).join(said))})")
        self.assertEqual(passed.returncode, 0, passed.stdout)
        kept = Path(gate_verdict.VERDICTS, "spent.txt").read_text().splitlines()
        self.assertIn(classes, kept)
        at = kept.index(classes)
        self.assertEqual(
            [line.split()[-1] for line in kept[at + 1 : at + 3]],
            ["hq.example.tests.test_pages.EveryPageTests", "hq.example.tests.test_rows.RowTests"],
        )
        self.assertNotIn("not a class time", "\n".join(kept))
        self.assertIn(f"[test] {measured}", kept)

    # A command that says what it judged in the file its gate hands it, and exits as told.
    SAYS_JUDGED = (
        "import os, sys; from pathlib import Path;"
        " Path(os.environ['HQ_GATE_DETAIL']).write_text('judged: example/one\\nat: 402\\n'); sys.exit(int(sys.argv[1]))"
    )

    def test_what_a_command_says_it_judged_ends_the_verdict_line_and_is_kept_under_it_whichever_way_it_ends(self):
        for code, outcome in ((1, "FAILED"), (0, "passed")):
            with self.subTest(outcome=outcome):
                result = self.gate("judging", sys.executable, "-c", self.SAYS_JUDGED, str(code))
                kept = Path(gate_verdict.VERDICTS, "judging.txt").read_text().splitlines()
                self.assertTrue(kept[0].startswith("tree "), kept)
                self.assertRegex(kept[1], rf"^\[judging\] {outcome} in [\d.]+s: judged: example/one")
                self.assertEqual(result.stdout.splitlines()[-1], kept[1])
                self.assertEqual(kept[2:4], ["judged: example/one", "at: 402"])

    def test_a_command_that_says_what_reruns_it_is_rerun_so_and_not_by_its_test_labels(self):
        says_again = (
            "import os, sys; from pathlib import Path;"
            " Path(os.environ['HQ_GATE_RERUN']).write_text('mise run dev:look --rule survey example/one\\n');"
            f" exec({SAYS!r})"
        )
        result = self.gate("again", sys.executable, "-c", says_again, "[]", "example.tests.Look.test_look")
        self.assertEqual(result.returncode, 1, result.stdout)
        closing = result.stdout.splitlines()[-1]
        self.assertTrue(closing.endswith("; rerun: mise run dev:look --rule survey example/one"), closing)


@tag("source")
class ConcurrentCloseTests(GateTestCase):
    """Two runs of one gate close at once, as a suite run under sh and dash does."""

    def test_a_verdict_another_run_moved_away_still_closes_and_is_kept(self):
        kept = Path(gate_verdict.VERDICTS, "raced.txt")
        seen = Path.exists

        def moved_after_the_look(path: Path, *args: object, **kwargs: object) -> bool:
            # The other run moves the verdict to .prev between this run's look and its move.
            return path == kept or seen(path, *args, **kwargs)

        with (
            patch.object(Path, "exists", moved_after_the_look),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            gate_verdict.Gate("raced").close([gate_verdict.Step("tests")])
        self.assertTrue(kept.read_text().splitlines()[1].startswith("[raced] passed in"))
        self.assertEqual([path.name for path in kept.parent.glob(".raced.*")], [])


class OpeningLineCase(GateTestCase):
    def assert_opens_on_where_its_verdict_is_kept(self, out: str) -> None:
        first, *_, last = out.splitlines()
        path = Path(first.rsplit(" ", 1)[-1])
        self.assertEqual(path.parent, gate_verdict.VERDICTS, first)
        self.assertEqual(path.read_text().splitlines()[1], last)


def step_said(label: str, cmd: list[str], *args: object, **kwargs: object) -> gate_verdict.Step:
    """A step that prints before it ends, as every real one does."""

    print("step output", flush=True)
    return gate_verdict.Step(label)


@tag("source", "tree")
class GateOpeningLineTests(OpeningLineCase):
    def test_a_long_failing_command_under_the_verdict_opens_on_its_file(self):
        """As a terminal or a CI job is shown it, all the command printed."""

        result = subprocess.run(
            [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), "example", sys.executable, "-c", LONG],
            cwd=ROOT,
            env=os.environ | {gate_verdict.OUTPUT: "all"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertGreater(len(result.stdout), 30_000)
        self.assert_opens_on_where_its_verdict_is_kept(result.stdout)

    def test_fast_opens_on_its_file(self):
        _, printed = run_main(("hq/example.py",), ("lint:ruff",), Recorder())
        self.assert_opens_on_where_its_verdict_is_kept("\n".join(printed))

    def test_check_opens_on_its_file_whichever_way_it_ends(self):
        def git(*args: str) -> list[str]:
            return ["0123abc"] if args[0] == "rev-parse" else []

        for asked, code in (({}, 0), ({"release": "true", "skip_host": "true"}, 2)):
            with (
                self.subTest(asked=asked),
                patch.object(check, "usage", return_value=Usage(asked)),
                patch.object(check, "git", side_effect=git),
                patch.object(check, "run", side_effect=step_said),
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                self.assertEqual(check.main(), code)
            self.assert_opens_on_where_its_verdict_is_kept(out.getvalue())

    def test_a_test_run_opens_on_its_file(self):
        with (
            patch.object(at, "run", side_effect=step_said),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            at.run_here(["example.tests"])
        self.assert_opens_on_where_its_verdict_is_kept(out.getvalue())


@tag("source")
class TestAtOpeningLineTests(OpeningLineCase):
    """`test --at` on the fixture repository its own tests run on."""

    setUp = TestAtRefTests.setUp
    git = TestAtRefTests.git
    write = TestAtRefTests.write
    commit = TestAtRefTests.commit
    run_main = TestAtRefTests.run_main

    def test_a_run_at_a_ref_opens_on_its_file(self):
        seen = Path(self.root.parent, "seen.json")
        with patch.object(at, "UV_RUN", [sys.executable, "-c", f"print('step output')\n{PROBE}"]):
            code, out = self.run_main(str(seen), "1", at_ref=self.old)
        self.assertEqual(code, 1)
        self.assert_opens_on_where_its_verdict_is_kept(out)


# The kept file's first line as the hand-back's reader reads it.
TREE_LINE = re.compile(r"^tree ([0-9a-f]{40}|[0-9a-f]{64})$")


@tag("source")
class JudgedTreeTests(GateTestCase):
    """The kept verdict opens on the tree the gate judged, the one a commit of the checkout's files holds, so a
    pass is attested by comparing it with a commit's tree; a checkout that changed in the run names none."""

    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The user's own ignore rules apply as a commit's do; this fixture's user ignores *.own.
        (home / "ignore").write_text("*.own\n")
        (home / "gitconfig").write_text(
            f"[user]\n\tname = Example\n\temail = dev@example.com\n[commit]\n\tgpgsign = false\n"
            f"[core]\n\texcludesFile = {home / 'ignore'}\n"
        )
        self.env = os.environ | {"GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q")
        (self.repo / ".gitignore").write_text("*.log\n")
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "--all")
        self.git("commit", "-q", "--no-verify", "-m", "a")

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=self.repo, env=self.env, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    def kept(self, *cmd: str) -> list[str]:
        """The kept lines of a gate run through verdict.py on the fixture's checkout, the verdict under the tree."""

        done = subprocess.run(
            [sys.executable, "-m", "tools.gate.verdict", "judged", *cmd],
            cwd=ROOT,
            env=self.env | {"HQ_GATE_TREE": str(self.repo)},
            check=False,
            capture_output=True,
            text=True,
        )
        lines = Path(gate_verdict.VERDICTS, "judged.txt").read_text().splitlines()
        self.assertEqual(lines[1], done.stdout.splitlines()[-1])
        return lines

    def test_a_clean_checkout_names_its_commits_tree(self):
        first = self.kept(sys.executable, "-c", "pass")[0]
        self.assertRegex(first, TREE_LINE)
        self.assertEqual(first, f"tree {self.git('rev-parse', 'HEAD^{tree}')}")

    def test_a_dirty_checkout_names_the_tree_a_commit_of_its_files_holds_and_leaves_the_index_alone(self):
        (self.repo / "a.txt").write_text("changed\n")
        (self.repo / "staged.txt").write_text("staged\n")
        self.git("add", "staged.txt")
        (self.repo / "staged.txt").write_text("staged, then changed\n")
        (self.repo / "untracked.txt").write_text("new\n")
        (self.repo / "run.log").write_text("ignored by the repository\n")
        (self.repo / "notes.own").write_text("ignored by the user\n")
        # The tree the index holds, which the gate's own copy of it leaves as it was.
        staged = self.git("write-tree")
        first = self.kept(sys.executable, "-c", "raise SystemExit(1)")[0]
        self.assertEqual(self.git("write-tree"), staged)
        self.git("add", "--all")
        self.git("commit", "-q", "--no-verify", "-m", "b")
        self.assertEqual(first, f"tree {self.git('rev-parse', 'HEAD^{tree}')}")

    def test_a_checkout_changed_in_the_run_names_no_tree(self):
        written = self.repo / "written.txt"
        self.assertEqual(self.kept(sys.executable, "-c", f"open({str(written)!r}, 'w').write('x')")[0], "tree -")
