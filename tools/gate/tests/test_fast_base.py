"""How fast picks the commit it reads the change from, and how it readies the
tree before its gates judge it: each selected gate's fix mode first."""

import io
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import tag

from hq.platform.application.tests.reads import reads
from hq.platform.core.tests.task import RefError
from tests.mise_tasks import ROOT, body, mise_tasks
from tools.gate import fast as fast_gate
from tools.gate.tests.base import GateTestCase, fixture_git
from tools.gate.tests.fast_runs import Recorder, run_main
from tools.gate.verdict import Step


@tag("source")
class FastGateBaseTests(GateTestCase):
    REFS = {"main": "aaa", "topic": "bbb"}

    def resolve(self, base, landing=()):
        def git(*args):
            if args[-1] not in self.REFS:
                raise RefError(f"no ref {args[-1]}")
            return [self.REFS[args[-1]]]

        with patch.object(fast_gate, "git", git), patch.object(fast_gate, "landing", return_value=list(landing)):
            return fast_gate.merge_base(base)

    def test_with_none_asked_for_the_branch_head_lands_on_is_the_base(self):
        self.assertEqual(self.resolve(None, landing=["topic"]), "bbb")

    def test_the_branch_a_worktree_lands_on_is_the_one_it_was_made_from(self):
        """A worktree's branch tracks nothing; the branch it was made from tracks a remote one and has moved on
        since. Its own upstream, where it has one, is the base."""
        with tempfile.TemporaryDirectory() as directory:
            origin, clone = Path(directory) / "origin", Path(directory) / "clone"
            origin.mkdir()
            git = fixture_git(origin)
            git("init", "-q", "-b", "main")
            git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "main")
            git("checkout", "-q", "-b", "integration")
            git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "ahead of main")
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
            git = fixture_git(clone)
            git("checkout", "-q", "-b", "local-only")
            git("checkout", "-q", "integration")
            git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "landed, not pushed")
            fork = git("rev-parse", "HEAD")
            self.assertEqual(fast_gate.landing(clone), ["origin/integration"])
            git("checkout", "-q", "-b", "worker")
            git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "the worker's change")
            git("checkout", "-q", "integration")
            git("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "another worker landed")
            git("checkout", "-q", "worker")
            (nearest,) = fast_gate.landing(clone)
            self.assertEqual(nearest, "refs/heads/integration")
            self.assertEqual(git("merge-base", "HEAD", nearest), fork)

    def test_a_base_asked_for_is_the_one_used(self):
        self.assertEqual(self.resolve("topic"), "bbb")

    def test_a_base_asked_for_that_does_not_resolve_fails(self):
        with self.assertRaises(SystemExit) as raised:
            self.resolve("typo")
        self.assertIn("typo", str(raised.exception.code))

    def test_the_environment_names_a_base_behind_the_argument(self):
        """CHECK_BASE and CHECK_PARALLEL reach the gate through the task's usage spec, never read by hand."""
        usage = mise_tasks()["fast"]["usage"]
        self.assertRegex(usage, r'arg "\[base\]" env="CHECK_BASE"')
        self.assertRegex(usage, r'flag "--parallel <\w+>" env="CHECK_PARALLEL"')

    def test_with_none_asked_for_a_missing_origin_falls_back_to_main(self):
        self.assertEqual(self.resolve(None), "aaa")


# A run of fast that selects nothing and runs no step.
QUIET = {"step": Recorder(), "affected": (set(), set()), "test_labels": ([], 0), "touches_shared_ui": False}


@tag("source")
class FastGatePassedBaseTests(GateTestCase):
    """With no base asked for, the change is read from the nearest commit of HEAD's first-parent history whose
    tree passed, so a pass of what it selects proves the tree; without one, from the merge base as before."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git = fixture_git(self.root)
        self.git("init", "-q", "-b", "main")
        self.commits = []
        for name in ("first", "second", "third"):
            (self.root / f"{name}.txt").write_text(f"{name}\n")
            self.git("add", "-A")
            self.git("commit", "--quiet", "--no-verify", "-m", name)
            self.commits.append(self.git("rev-parse", "HEAD"))
        self.enterContext(patch.object(fast_gate, "ROOT", self.root))
        self.merge_base = self.enterContext(patch.object(fast_gate, "merge_base", return_value="f" * 40))

    def record(self, commit: str) -> None:
        folder = fast_gate.passes(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / self.git("rev-parse", f"{commit}^{{tree}}")).touch()

    def test_the_nearest_commit_whose_tree_passed_is_the_base(self):
        first, second, _ = self.commits
        self.record(first)
        self.record(second)
        base, why = fast_gate.fast_base(None)
        self.assertEqual(base, second)
        self.assertIn("passed", why)
        self.merge_base.assert_not_called()

    def test_with_no_passed_tree_in_reach_the_merge_base_is_the_base(self):
        for recorded in (False, True):
            with self.subTest(recorded=recorded):
                if recorded:
                    # A passed tree off HEAD's history is no base of this change.
                    fast_gate.passes(self.root).mkdir(parents=True, exist_ok=True)
                    (fast_gate.passes(self.root) / ("e" * 40)).touch()
                base, why = fast_gate.fast_base(None)
                self.assertEqual(base, "f" * 40)
                self.assertIn("merge base", why)

    def test_a_base_asked_for_wins_over_a_passed_tree(self):
        self.record(self.commits[-1])
        base, _ = fast_gate.fast_base("main")
        self.assertEqual(base, "f" * 40)
        self.merge_base.assert_called_once_with("main")

    def test_the_line_naming_the_base_says_why_it_is_the_one(self):
        _, printed = run_main(("hq/example.py",), passed_ancestor=(self.commits[1], 1), **QUIET)
        (line,) = [line for line in printed if line.startswith("[fast] base ")]
        self.assertIn(self.commits[1][:9], line)
        self.assertIn("passed", line)


@tag("source")
class FastGateFixTests(GateTestCase):
    """Before the gates judge the tree, each selected quick gate with a fix mode runs it, so a run never fails
    on what the tool rewrites itself, and the run judges the tree the fixes left."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git = fixture_git(self.root)
        git("init", "-q")
        for name in ("changed.py", "clean.py", "notes.txt"):
            (self.root / name).write_text("before\n")
        git("add", "-A")
        git("commit", "--quiet", "--no-verify", "-m", "base")
        (self.root / "changed.py").write_text("unformatted\n")
        (self.root / "notes.txt").write_text("edited, never fixed\n")
        header = self.root / "file-task"
        header.write_text('#!/bin/sh\n#MISE hide=true\n#USAGE flag "--fix" help="Write the fixes"\n')
        self.tasks = {
            "lint:toml": {"name": "lint:toml", "usage": 'flag "--fix" help="Write the fixes"', "file": None},
            "lint:file": {"name": "lint:file", "usage": "", "file": str(header)},
            "lint:check": {"name": "lint:check", "usage": "", "file": None},
            "lint:record": {
                "name": "lint:record",
                "usage": 'flag "--fix" help="Record over the file"',
                "file": None,
                "tags": ["fast", fast_gate.RECORDING],
            },
        }
        self.enterContext(patch.object(fast_gate, "ROOT", self.root))
        self.enterContext(patch.object(fast_gate, "quick_tasks", return_value=self.tasks))

    def test_a_gate_declares_a_fix_mode_in_its_usage_whether_toml_or_file_task(self):
        self.assertEqual(fast_gate.fixers(("lint:toml", "lint:file", "lint:check")), ("lint:toml", "lint:file"))

    def test_the_fix_modes_run_alone_and_what_they_rewrote_is_named(self):
        ran: list[list[str]] = []

        def run(command, **_):
            ran.append(command)
            for name in ("changed.py", "clean.py"):
                (self.root / name).write_text("formatted\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(fast_gate, "subprocess", SimpleNamespace(run=run)):
            fixed = fast_gate.fix(("lint:toml", "lint:check", "lint:file"))
        self.assertEqual(fixed, ["changed.py", "clean.py"])
        (command,) = ran
        self.assertEqual(command[:2], ["mise", "run"])
        words = " ".join(command[2:])
        self.assertIn("lint:toml --fix", words)
        self.assertIn("lint:file --fix", words)
        self.assertNotIn("lint:check", words)

    def test_a_fix_that_rewrites_a_change_back_to_head_is_named(self):
        """Ruff writes `str()` back as `""`, which can leave the file as HEAD has it: no change is left to read."""

        def run(command, **_):
            (self.root / "changed.py").write_text("before\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(fast_gate, "subprocess", SimpleNamespace(run=run)):
            self.assertEqual(fast_gate.fix(("lint:toml",)), ["changed.py"])

    def test_a_recordings_fix_never_runs_before_the_gates(self):
        """Its fix is its whole recording again, which every gate would wait on."""
        with (
            patch.object(fast_gate, "fix", return_value=[]) as fix,
            patch.object(fast_gate, "changed_paths") as changed,
        ):
            paths = fast_gate.fixed_first(SimpleNamespace(), ("lint:toml", "lint:record"), "base", (["changed.py"], []))
        fix.assert_called_once_with(("lint:toml",))
        self.assertEqual(paths, (["changed.py"], []))
        changed.assert_not_called()

    def after(self, first, fixed=("examples.json",)):
        """fixed_after on a first step, its fix rewriting ``fixed``: the steps, the fix's calls, the run again's."""
        gate = Mock()
        again = Step("again")
        with (
            patch.object(fast_gate, "fix", return_value=list(fixed)) as fix,
            patch.object(fast_gate, "selected_tasks", return_value=("lint:record", "lint:doc")) as selected,
            patch.object(fast_gate, "affected", return_value=(set(), set())),
            patch.object(fast_gate, "test_labels", return_value=(["tests.test_examples"], 1)),
            patch.object(fast_gate, "with_tree", side_effect=lambda labels, paths: labels),
            patch.object(fast_gate, "gates_and_tests", return_value=[again]) as rerun,
            patch("sys.stdout", io.StringIO()),
        ):
            steps = fast_gate.fixed_after(gate, [first], [], "auto")
        return steps, fix, selected, rerun, gate

    def test_a_failed_recording_is_fixed_and_what_the_fix_rewrote_is_judged_again(self):
        steps, fix, selected, rerun, gate = self.after(Step("first", ok=False, code=1, tasks=["lint:record"]))

        fix.assert_called_once_with(("lint:record",))
        selected.assert_called_once_with(["examples.json"], "HEAD")
        (tasks, labels, *_), _ = rerun.call_args
        self.assertEqual(tasks, ("lint:record", "lint:doc"))
        self.assertEqual(labels, ["tests.test_examples"])
        gate.restart.assert_called_once_with()
        # The first run failed on the recording alone, so the run again decides.
        self.assertTrue(steps[0].ok)
        self.assertEqual([step.label for step in steps], ["first", "again"])

    def test_a_failure_beside_the_recordings_still_fails_the_run(self):
        first = Step("first", ok=False, code=1, tasks=["lint:record", "lint:check"])
        steps, *_ = self.after(first)
        self.assertFalse(steps[0].ok)
        self.assertEqual(steps[0].tasks, ["lint:record", "lint:check"])

    def test_a_passing_run_or_a_fix_that_rewrote_nothing_runs_nothing_again(self):
        for first, fixed in ((Step("first"), ("examples.json",)), (Step("first", ok=False, tasks=["lint:record"]), ())):
            with self.subTest(failed=first.tasks, fixed=fixed):
                steps, fix, _, rerun, gate = self.after(first, fixed)
                self.assertEqual(steps, [first])
                rerun.assert_not_called()
                gate.restart.assert_not_called()
                self.assertEqual(fix.called, bool(first.tasks))

    def test_no_selected_fix_mode_runs_nothing(self):
        with patch.object(fast_gate, "subprocess", SimpleNamespace(run=self.fail)):
            self.assertEqual(fast_gate.fix(("lint:check",)), [])

    def test_a_run_that_fixed_says_so_and_judges_the_fixed_tree(self):
        for fixed in ([], ["changed.py"]):
            with self.subTest(fixed=fixed), patch.object(fast_gate.Gate, "restart") as restart:
                _, printed = run_main(("changed.py",), fix=fixed, **QUIET)
                said = [line for line in printed if "fixes rewrote" in line]
                self.assertEqual(bool(said), bool(fixed))
                self.assertTrue(all("changed.py" in line for line in said))
                self.assertEqual(restart.called, bool(fixed))


# A task named with the fix flag after it, as a run passes it.
FIXES = re.compile(r"\b(lint:[\w:-]+) --fix\b")


@tag("source")
class FastGateFixModeTests(GateTestCase):
    def test_the_quick_gates_a_fixture_lists_never_answer_for_this_checkout(self):
        """A test that runs fast on a fixture lists the fixture's gates, and the next run in its process reads
        this checkout's, in either order."""
        for first in ("checkout", "fixture"):
            with self.subTest(first=first):
                if first == "checkout":
                    self.assertIn("lint:ruff", fast_gate.quick_gates())
                with (
                    tempfile.TemporaryDirectory() as folder,
                    patch.object(fast_gate, "ROOT", Path(folder)),
                    patch.object(fast_gate, "mise_json", return_value=[]),
                ):
                    self.assertEqual(fast_gate.quick_gates(), ())
                self.assertIn("lint:ruff", fast_gate.quick_gates())

    def test_each_gate_that_knows_its_own_fix_has_a_fix_mode(self):
        """The gates whose tool rewrites what it finds (Ruff, Stylelint, ESLint, mise fmt, ast-grep and the Go
        controller's) and every derived file's, so fast writes the fix where a worker would otherwise fail on it."""
        quick = fast_gate.quick_gates()
        fixable = set(fast_gate.fixers(quick))
        tools = {"lint:ruff", "lint:css", "lint:js", "lint:mise-format", "lint:ast-grep", "lint:controller"}
        derived = {gate for gate in quick if gate.startswith("lint:drift-")}
        self.assertTrue(derived)
        self.assertLessEqual(tools | derived, fixable)

    def test_each_recording_names_the_tests_it_records(self):
        """fast selects a recording by the tests its run names, so one that names none would run on every change."""
        recordings = fast_gate.recordings()
        self.assertTrue(recordings)
        for name in recordings:
            with self.subTest(name):
                self.assertTrue(fast_gate.recorded_labels(name), f"{name}'s run names no test labels")


@tag("source", "tree")
@reads(".github/**", "mise-tasks/**", "mise*.toml", "mise.lock")
class FixFlagPasserTests(GateTestCase):
    def test_only_fast_and_fix_pass_the_fix_flag(self):
        """A fix mode writes the tree, so a run that judges a tree it cannot change (CI's check, its lint job)
        never passes the flag: only fast, before it judges, and `mise run fix`, which an operator runs."""
        passers = set()
        for name, task in mise_tasks().items():
            # A gate's failure may name its own fix, for whoever reads it.
            if set(FIXES.findall(body(task))) - {name}:
                passers.add(name)
        for workflow in (ROOT / ".github").rglob("*.y*ml"):
            if FIXES.search(workflow.read_text(encoding="utf-8")):
                passers.add(str(workflow.relative_to(ROOT)))
        self.assertLessEqual(passers, {"fast", "fix", "fix:derived"})
        self.assertIn("fix:derived", passers)
