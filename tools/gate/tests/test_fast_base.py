"""How fast picks the commit it reads the change from, and how it readies the
tree before its gates judge it: each selected gate's fix mode first."""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import tag

from hq.platform.core.tests.task import RefError
from tests.mise_tasks import mise_tasks
from tools.gate import fast as fast_gate
from tools.gate.tests.base import GateTestCase, fixture_git
from tools.gate.tests.fast_runs import Recorder, run_main


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


@tag("source")
class FastGateFixModeTests(GateTestCase):
    def test_each_format_gate_has_a_fix_mode(self):
        """The gates whose tool rewrites what it finds: Ruff, Stylelint, ESLint and mise fmt."""
        fixable = set(fast_gate.fixers(fast_gate.quick_gates()))
        self.assertLessEqual({"lint:ruff", "lint:css", "lint:js", "lint:mise-format"}, fixable)
