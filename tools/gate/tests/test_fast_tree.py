"""fast runs each test tagged tree that a change reaches: one that declares what it reads (reads.py) on a
change to those files, the files its programs read included, and any other on every change."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag
from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases

from hq.platform.application.tests.tree import SUITE
from hq.platform.core.test_runner import excluded_tags
from tests.mise_tasks import mise_tasks
from tools.gate import chain as fast_chain, fast as fast_gate, selection as fast_select
from tools.gate.tests.base import GateTestCase, fixture_git
from tools.gate.tests.fast_runs import everywhere

ROOT = Path(__file__).resolve().parents[3]


@tag("source")
class FastGateTreeTests(GateTestCase):
    @tag("tree")
    def test_a_change_of_any_kind_reaches_a_tree_test_that_declares_nothing_and_none_does_not(self):
        """A walk reads a new test module, a template and a doc alike, by no import and no name."""
        modules = [f"{package}/example/tests/test_example.py" for package in SUITE]
        others = ["static/css/app.css", "templates/base.html", "docs/Example.md", ".github/workflows/ci.yml"]
        with everywhere("tree_tests", return_value=[("hq.a.tests.test_tree.Walks", None)]):
            for path in [*modules, *others]:
                with self.subTest(path=path):
                    self.assertIn("hq.a.tests.test_tree.Walks", fast_select.with_tree([], [path]))
            self.assertEqual(fast_select.with_tree([], []), [])

    @tag("tree")
    def test_a_tree_test_that_declares_what_it_reads_runs_on_a_change_to_those_files_alone(self):
        """An edit reaches a file it opens, a program it starts included; only an added or deleted file
        reaches one it lists."""
        walks = "hq.a.tests.test_tree.Walks"
        declared = [(walks, (("templates/**/*.html",), ("hq/**/*.py",)))]
        with everywhere("tree_tests", return_value=declared):
            for paths, listed in (
                (["templates/partials/table.html"], []),
                (["templates/new.html"], ["templates/new.html"]),
                (["hq/domains/example/views.py"], ["hq/domains/example/views.py"]),
            ):
                with self.subTest(paths=paths, listed=listed):
                    self.assertEqual(fast_select.with_tree([], paths, listed), [walks])
            for paths, listed in (
                (["hq/domains/example/views.py"], []),
                (["static/css/app.css", "docs/Example.md"], ["docs/Example.md"]),
            ):
                with self.subTest(paths=paths, listed=listed):
                    self.assertEqual(fast_select.with_tree([], paths, listed), [])
        # The watch holds a program the test starts to the declaration by what it reads, so each file read
        # selects the test.
        from hq.platform.application.tests.reads import Reads, watching

        held = (("tools/gate/**/*.py",), ("tools/gate/**",))
        rule = '{"id": "x", "language": "Python", "rule": {"pattern": "x()"}}'
        scan = ["ast-grep", "scan", "--inline-rules", rule, "--json=stream", "tools/gate"]
        with watching(Reads(*held)):
            subprocess.run(scan, cwd=ROOT, capture_output=True, check=True)
        read = subprocess.run(
            [*scan, "--inspect", "entity"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stderr.split()
        files = [word.removeprefix("entity|file|").removesuffix(":") for word in read if "entity|file|" in word]
        self.assertTrue(files, "the scan names no file")
        added = "tools/gate/example/notes.txt"
        with everywhere("tree_tests", return_value=[(walks, held)]):
            for path in files:
                with self.subTest(path=path):
                    self.assertEqual(fast_select.with_tree([], [path], []), [walks])
            self.assertEqual(fast_select.with_tree([], [added], [added]), [walks])
            self.assertEqual(fast_select.with_tree([], ["tools/gate/README.md"], []), [])

    @tag("tree")
    def test_a_change_adds_each_tree_test_it_reaches_to_the_one_run(self):
        from hq.platform.application.tests.tree import tree_tests

        tree = tree_tests(ROOT)
        undeclared = {label for label, held in tree if held is None}
        self.assertIn("hq.platform.application.tests.test_tree_tag.TreeTagTests", undeclared)
        # The walk above is this checkout's; with_tree asks for it once and adds what it answers.
        with everywhere("tree_tests", return_value=tree) as walked:
            labels = fast_select.with_tree(["hq.a.tests.test_b"], ["hq/domains/example/models.py"], [])
        walked.assert_called_once_with(fast_chain.ROOT)
        self.assertLessEqual({"hq.a.tests.test_b", *undeclared}, set(labels))
        command = fast_gate.run_command((), [], labels, "2")
        self.assertLessEqual(set(labels), set(command))
        self.assertNotIn("--tag", command)
        self.assertNotIn("--exclude-tag", command)
        # A labeled run keeps the source tests a bare whole-suite run leaves out.
        self.assertEqual(excluded_tags(labels, set(), set()), set())

    @tag("tree")
    def test_the_walk_started_beside_the_selection_is_the_one_read(self):
        walks = [("hq.a.tests.test_tree.Walks", None)]
        with everywhere("tree_tests", return_value=walks) as walked:
            walking = fast_select.walk_for_tree()
            labels = fast_select.with_tree(["hq.a.tests.test_b"], ["docs/Example.md"], None, walking)
        walked.assert_called_once_with(fast_chain.ROOT)
        self.assertEqual(labels, ["hq.a.tests.test_b", "hq.a.tests.test_tree.Walks"])

    def test_the_paths_a_listing_can_tell_are_those_added_or_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            git("init", "--quiet")
            for name in ("kept.txt", "edited.txt", "deleted.txt"):
                (root / name).write_text("base\n")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            (root / "edited.txt").write_text("edited\n")
            (root / "deleted.txt").unlink()
            (root / "staged.txt").write_text("new\n")
            git("add", "staged.txt")
            (root / "untracked.txt").write_text("new\n")
            with patch.object(fast_chain, "ROOT", root):
                listed = fast_select.listed_change(base, ["deleted.txt"])
        self.assertEqual(listed, ["deleted.txt", "staged.txt", "untracked.txt"])

    def test_a_test_both_selected_and_tagged_tree_runs_once(self):
        module = "tools.gate.tests.test_turn"
        once = DiscoverRunner(verbosity=0).build_suite([module])
        twice = DiscoverRunner(verbosity=0).build_suite([module, *(test.id() for test in iter_test_cases(once))])
        self.assertGreater(once.countTestCases(), 1)
        self.assertEqual(twice.countTestCases(), once.countTestCases())

    def test_fast_installs_the_lint_tools_its_gates_and_tests_run(self):
        self.assertIn("setup:lint-tools", mise_tasks()["fast"]["depends"])
