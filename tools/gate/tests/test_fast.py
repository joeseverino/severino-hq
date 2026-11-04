"""The inner-loop gate maps a change to the tests and quick gates that reach
it, runs the light gates beside the tests and those that take the heavy turn
after them, and names every failed step in its verdict."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from unittest.mock import patch

from django.test import tag
from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases

from hq.config.sandbox import PREFIX, sweep_abandoned
from hq.platform.application.tests.tree import SUITE, tagged
from hq.platform.core.test_runner import excluded_tags
from hq.platform.core.tests.task import RefError, Usage, names, trusted_mise
from tests.mise_tasks import mise_tasks, takes_the_turn
from tools.gate import fast as fast_gate, verdict as gate_verdict
from tools.gate.tests.base import UNITTEST, GateTestCase
from tools.gate.turn import IN_TURN
from tools.gate.verdict import FailedTest

ROOT = Path(__file__).resolve().parents[3]
# The shell gate's own lister, which fast asks rather than restating.
SOURCES = ROOT / "tools" / "gate" / "shell-sources.sh"
IDENTITY = ["-c", "user.name=Example", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]


def fixture_git(root: Path) -> Callable[..., str]:
    """git in the fixture repository at ``root``, committing as an example identity."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", *IDENTITY, *args], cwd=root, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    return git


class Recorder:
    """A step that prints before it ends, as every real one does, and records what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, label: str, cmd: list[str]) -> gate_verdict.Step:
        self.calls.append((label, cmd))
        print("step output", flush=True)
        return gate_verdict.Step(label)


def run_main(
    changed: tuple[str, ...] = (), selected: tuple[str, ...] = (), step: Any = None, **fakes: Any
) -> tuple[int, list[str]]:
    """fast's run on a change to ``changed``, its quick gates ``selected``, each step through ``step``
    when given: its exit code and printed lines."""

    fakes = {"merge_base": "0" * 40, "changed_paths": (list(changed), []), "selected_tasks": selected, **fakes}
    out = io.TextIOWrapper(io.BytesIO(), write_through=True)
    with ExitStack() as stack:
        for name, value in fakes.items():
            stack.enter_context(patch.object(fast_gate, name, return_value=value))
        if step is not None:
            stack.enter_context(patch.object(fast_gate, "step", side_effect=step))
        stack.enter_context(patch("sys.stdout", new=out))
        code = fast_gate.main(Usage({}))
    return code, out.buffer.getvalue().decode().splitlines()


@tag("source", "tree")
class FastGateMappingTests(GateTestCase):
    def setUp(self):
        self.files = fast_gate.python_files()

    def labels(self, *changed: str) -> list[str]:
        reached, touched = fast_gate.affected(list(changed), [], self.files)
        return fast_gate.test_labels(self.files, reached, touched)[0]

    def test_a_package_init_is_the_package(self):
        self.assertEqual(fast_gate.module_name(Path("a/b/__init__.py")), "a.b")
        self.assertEqual(fast_gate.module_name(Path("a/b/c.py")), "a.b.c")

    def test_a_relative_import_reaches_its_sibling(self):
        importers = fast_gate.importers(self.files)
        self.assertIn("hq.platform.application.tests.test_domains", importers["hq.platform.application.domains"])

    def test_a_declarations_string_reference_is_not_an_import(self):
        importers = fast_gate.importers(self.files)
        self.assertNotIn("hq.platform.application.domains", importers["hq.domains.expenses.urls"])

    def test_a_lazy_import_and_a_tests_mock_target_are_imports_and_reach_the_package(self):
        modules = {
            "example/__init__.py": "",
            "example/models.py": "class Thing: ...\n",
            "example/urls.py": "patterns = []\n",
            "example/lazy.py": "lazy from example.models import Thing\n",
            "example/declares.py": 'URLCONF = "example.urls.patterns"\n',
            "example/tests/__init__.py": "",
            "example/tests/test_mock.py": '@patch("example.urls.patterns")\ndef test(): ...\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in modules.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            with patch.object(fast_gate, "ROOT", root):
                importers = fast_gate.importers([Path(path) for path in modules])
        self.assertEqual(importers["example.models"], {"example.lazy"})
        self.assertEqual(importers["example.urls"], {"example.tests.test_mock"})
        self.assertLessEqual({"example.lazy", "example.tests.test_mock"}, importers["example"])

    def test_an_app_change_selects_the_tests_that_use_the_app(self):
        labels = self.labels("hq/domains/expenses/views.py")
        self.assertIn("hq.platform.application.tests.test_domains", labels)
        self.assertNotIn("hq.platform.mcp.tests", labels)

    def test_a_model_change_selects_its_app_and_not_an_unrelated_domain(self):
        labels = self.labels("hq/domains/projects/models.py")
        own = [
            fast_gate.module_name(p)
            for p in self.files
            if fast_gate.is_test(p) and p.parts[:3] == ("hq", "domains", "projects")
        ]
        self.assertTrue(own)
        self.assertLessEqual(set(own), set(labels))
        self.assertFalse([label for label in labels if label.startswith("hq.domains.receipts.")])

    def test_an_app_label_in_prose_is_not_a_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {
                "namespace": 'reverse("projects:list")',
                "model": 'apps.get_model("projects", "Project")',
                "prose": '"""Covers the projects page and its core."""',
            }.items():
                (root / f"test_{name}.py").write_text(text)
            files = sorted(Path(p.name) for p in root.iterdir())
            with patch.object(fast_gate, "ROOT", root):
                chosen, _ = fast_gate.test_labels(files, set(), {"projects"})
            self.assertEqual(chosen, ["test_model", "test_namespace"])

    def test_a_changed_file_selects_the_tests_that_name_its_path_or_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {
                "string": 'read(".github/actions/example/action.yml")',
                "segments": 'root / ".github" / "actions" / "example" / "action.yml"',
                "arguments": 'Path(".github", "actions", "example")',
                "longer": 'read(".github/actions/example-other/action.yml")',
                "other": 'read(".github/actions/elsewhere/action.yml")',
            }.items():
                (root / f"test_{name}.py").write_text(text)
            files = sorted(Path(p.name) for p in root.iterdir())
            with patch.object(fast_gate, "ROOT", root):
                chosen, _ = fast_gate.test_labels(files, set(), set(), [".github/actions/example/action.yml"])
            self.assertEqual(chosen, ["test_arguments", "test_segments", "test_string"])

    def test_a_task_definition_change_selects_the_tests_that_read_the_tasks(self):
        """A task moved between mise.toml and mise-tasks/ is read through tests.mise_tasks, by no path."""
        for changed in ("mise.toml", "mise-tasks/browser.toml", "mise-tasks/test/composed"):
            with self.subTest(changed=changed):
                self.assertIn("hq.platform.core.tests.test_browser_gate_tasks", self.labels(changed))
        self.assertNotIn("hq.platform.core.tests.test_browser_gate_tasks", self.labels("docs/Development.md"))

    def test_a_change_to_a_file_a_task_runs_selects_the_tests_that_read_the_tasks(self):
        """GateReferenceTests reads the scripts and modules a task's run names, by no import of them."""
        reader = "hq.platform.application.tests.test_pipeline"
        for changed in ("tools/dev/dev.sh", "tools/gate/fast.py", "tools/structural/bar.py"):
            with self.subTest(changed=changed):
                self.assertIn(reader, self.labels(changed))
        self.assertNotIn(reader, self.labels("tools/gate/turn.py"))

    def test_a_test_change_selects_that_test_and_the_architecture_tests(self):
        labels = self.labels("hq/platform/core/tests/test_security.py")
        self.assertIn("hq.platform.core.tests.test_security", labels)
        self.assertIn("hq.platform.application.tests.test_architecture", labels)

    def test_a_template_belongs_to_its_app(self):
        _, touched = fast_gate.affected(["templates/expenses/list.html"], [], self.files)
        self.assertEqual(touched, {"expenses"})

    def test_a_deleted_module_selects_what_imported_it(self):
        reached, touched = fast_gate.affected([], ["hq/domains/projects/models.py"], self.files)
        self.assertEqual(touched, {"projects"})
        self.assertIn("hq.domains.reports.exports", reached)

    def test_apps_are_derived_from_the_tree(self):
        self.assertLessEqual({"expenses", "core", "projects"}, set(fast_gate.django_apps().values()))


@tag("source", "tree")
class FastGateTaskSelectionTests(GateTestCase):
    def test_every_quick_gate_declares_paths_that_name_files_of_this_tree(self):
        files = names("ls-files", root=ROOT)
        for task, patterns in fast_gate.TASKS.items():
            with self.subTest(task=task):
                self.assertTrue(patterns, f"{task} declares no paths, so fast would never run it")
                for pattern in patterns:
                    self.assertTrue(any(fnmatch(path, pattern) for path in files), f"{task}: {pattern} names no file")

    def test_the_quick_gates_are_tasks_and_every_drift_gate_is_one(self):
        tasks = mise_tasks()
        self.assertLessEqual(set(fast_gate.TASKS), set(tasks))
        drift = {name for name in tasks if name.startswith("lint:drift-")}
        self.assertTrue(drift, "no task is named lint:drift-*, so this holds nothing")
        self.assertLessEqual(drift, set(fast_gate.TASKS), "a drift gate is a quick gate: name its paths in TASKS")

    def test_a_docs_change_runs_only_the_prose_gate_and_the_gate_that_derives_it(self):
        self.assertEqual(set(fast_gate.selected_tasks(["docs/Example.md"])), {"lint:prose"})
        self.assertEqual(
            set(fast_gate.selected_tasks(["docs/Connections.md"])), {"lint:prose", "lint:drift-connections-reference"}
        )

    def test_a_change_to_a_file_the_shell_gate_reads_runs_it_and_no_other_change_does(self):
        """A workflow's run blocks are read by the shell gate's contracts, as its scripts and suites are."""
        for changed in (
            ".github/workflows/deploy.yml",
            ".github/actions/admit-plugin/action.yml",
            "tools/lib/heavy-turn.sh",
            "tests/shell/test-heavy.sh",
            "mise-tasks/lint/shell/_default",
        ):
            with self.subTest(changed=changed):
                self.assertIn("lint:shell", fast_gate.selected_tasks([changed]))
        for changed in ("docs/Example.md", "tools/gate/fast.py", "controller/host/verify.go"):
            with self.subTest(changed=changed):
                self.assertNotIn("lint:shell", fast_gate.selected_tasks([changed]))

    def test_a_python_change_runs_the_python_gates_and_not_the_stylesheet_ones(self):
        selected = fast_gate.selected_tasks(["hq/domains/docs_index/importer.py"])
        self.assertLessEqual({"lint:ruff", "lint:mypy", "test:migrations", "lint:drift-api-document"}, set(selected))
        self.assertNotIn("lint:css", selected)
        self.assertNotIn("lint:drift-font", selected)

    def test_a_change_to_tests_alone_leaves_out_the_structural_bar_which_never_reads_them(self):
        self.assertNotIn("lint:structural", fast_gate.selected_tasks(["hq/platform/core/tests/test_example.py"]))
        self.assertIn("lint:structural", fast_gate.selected_tasks(["hq/platform/core/example.py"]))
        both = ["hq/platform/core/tests/test_example.py", "hq/platform/core/example.py"]
        self.assertIn("lint:structural", fast_gate.selected_tasks(both))

    def test_a_change_to_what_every_gate_reads_runs_them_all(self):
        for changed in ("uv.lock", "mise-tasks/lint.toml"):
            with self.subTest(changed=changed):
                self.assertEqual(set(fast_gate.selected_tasks([changed])), {*fast_gate.TASKS, "lint:shell"})


@tag("source")
class FastGateTreeTests(GateTestCase):
    def test_a_module_of_any_package_the_suite_lives_in_reaches_the_tree_tests(self):
        # A new test file is one the walks read and no import reaches: the
        # source scans read it, and the tree tag's guard reads its tests.
        modules = ["hq/domains/example/models.py", "hq_sdk/ui.py", "hq/platform/core/tests/task.py"]
        modules += [f"{package}/example/tests/test_example.py" for package in SUITE]
        for path in modules:
            with self.subTest(path=path):
                self.assertTrue(fast_gate.reaches_tree(["docs/Example.md", path]))
        for path in ("static/css/app.css", "templates/base.html", "docs/Example.md"):
            with self.subTest(path=path):
                self.assertFalse(fast_gate.reaches_tree([path]))

    @tag("tree")
    def test_a_module_change_adds_every_tree_test_to_the_one_run(self):
        tree = tagged(ROOT, "tree")
        self.assertIn("hq.platform.application.tests.test_tree_tag.TreeTagTests", tree)
        labels = fast_gate.with_tree(["hq.a.tests.test_b"], ["hq/domains/example/models.py"])
        self.assertEqual(set(labels), {"hq.a.tests.test_b", *tree})
        self.assertEqual(fast_gate.with_tree(["hq.a.tests.test_b"], ["docs/Example.md"]), ["hq.a.tests.test_b"])
        command = fast_gate.test_command(labels, "2")
        self.assertLessEqual(set(labels), set(command))
        self.assertNotIn("--tag", command)
        self.assertNotIn("--exclude-tag", command)
        # A labeled run keeps the source tests a bare whole-suite run leaves out.
        self.assertEqual(excluded_tags(labels, set(), set()), set())

    def test_a_test_both_selected_and_tagged_tree_runs_once(self):
        module = "tools.gate.tests.test_turn"
        once = DiscoverRunner(verbosity=0).build_suite([module])
        twice = DiscoverRunner(verbosity=0).build_suite([module, *(test.id() for test in iter_test_cases(once))])
        self.assertGreater(once.countTestCases(), 1)
        self.assertEqual(twice.countTestCases(), once.countTestCases())

    def test_fast_installs_the_lint_tools_its_gates_and_tests_run(self):
        self.assertIn("setup:lint-tools", mise_tasks()["fast"]["depends"])


@tag("source")
class FastGateBaseTests(GateTestCase):
    REFS = {"main": "aaa", "topic": "bbb"}

    def resolve(self, base):
        def git(*args):
            if args[-1] not in self.REFS:
                raise RefError(f"no ref {args[-1]}")
            return [self.REFS[args[-1]]]

        with patch.object(fast_gate, "git", git):
            return fast_gate.merge_base(base)

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


@tag("source")
class FastGateNestedLayoutTests(GateTestCase):
    def test_app_config_label_can_differ_from_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path("hq/domains/example/apps.py")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text(
                'class ExampleConfig:\n    name = "hq.domains.example"\n    label = "stable_label"\n'
            )
            with patch.object(fast_gate, "ROOT", root), patch.object(fast_gate, "python_files", return_value=[path]):
                apps = fast_gate.django_apps()
            self.assertEqual(apps, {"hq/domains/example": "stable_label"})
            self.assertEqual(fast_gate.owner("hq/domains/example/views.py", apps), "stable_label")
            self.assertEqual(fast_gate.owner("templates/stable_label/list.html", apps), "stable_label")
            self.assertEqual(fast_gate.owner("static/stable_label/site.css", apps), "stable_label")

    def test_an_app_is_each_class_naming_one_in_an_apps_module_under_hq(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = {
                "hq/domains/example/apps.py": (
                    "import os\n"
                    "class Helper:\n    verbose = 3\n"
                    'class ExampleConfig:\n    default_auto_field = os.sep\n    name = "hq.domains.example"\n'
                    'class OtherConfig:\n    name, label = "hq.domains.other", "ignored"\n'
                ),
                "tools/example/apps.py": 'class ToolConfig:\n    name = "tools.example"\n',
            }
            for path, text in modules.items():
                (root / path).parent.mkdir(parents=True)
                (root / path).write_text(text)
            with (
                patch.object(fast_gate, "ROOT", root),
                patch.object(fast_gate, "python_files", return_value=[Path(path) for path in modules]),
            ):
                apps = fast_gate.django_apps()
            # Without a label of its own, an app is labeled by its name's last part.
            self.assertEqual(apps, {"hq/domains/example": "example"})

    def test_moved_untracked_sources_are_included_and_deleted_sources_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path("hq/domains/example/views.py")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text("")
            with (
                patch.object(fast_gate, "ROOT", root),
                patch.object(fast_gate, "names", return_value=[str(path), "example/views.py"]),
            ):
                self.assertEqual(fast_gate.python_files(), [path])

    def test_a_name_git_quotes_in_a_list_of_lines_reaches_the_selection_as_it_is(self):
        """systemd's escaping writes a backslash into a unit's name, which git quotes unless asked for -z."""

        added, new, gone, module = (rf"deploy/run\x2d{name}" for name in ("added.mount", "new.mount", "gone", "x.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            git("init", "--quiet")
            (root / "deploy").mkdir()
            (root / gone).write_text("gone\n")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            (root / gone).unlink()
            (root / added).write_text("added\n")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "change")
            (root / new).write_text("new\n")
            (root / module).write_text("")
            with patch.object(fast_gate, "ROOT", root):
                self.assertEqual(fast_gate.changed_paths(base, root), (sorted([added, new, module]), [gone]))
                self.assertEqual(fast_gate.python_files(), [Path(module)])

    def test_a_renamed_files_old_path_selects_what_reached_it(self):
        """git sees an unchanged file moved as a rename, which names only its new path unless asked not to."""

        files = {
            "hq/__init__.py": "",
            "hq/domains/__init__.py": "",
            "hq/domains/example/__init__.py": "",
            "hq/domains/example/apps.py": 'class ExampleConfig:\n    name = "hq.domains.example"\n',
            "hq/domains/example/models.py": "class Thing: ...\n",
            "hq/domains/example/moved.py": "VALUE = 1\n",
            "hq/domains/other/__init__.py": "",
            "hq/domains/other/apps.py": 'class OtherConfig:\n    name = "hq.domains.other"\n',
            "deploy/old.yml": "example: true\n",
            "tests/__init__.py": "",
            "tests/test_app.py": "from hq.domains.example.models import Thing\n",
            "tests/test_reads.py": 'PATH = "deploy/old.yml"\n',
            "tests/test_unrelated.py": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            for path, text in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            git("init", "--quiet")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("mv", "hq/domains/example/moved.py", "hq/domains/other/moved.py")
            git("mv", "deploy/old.yml", "deploy/new.yml")
            git("commit", "--quiet", "--no-verify", "-m", "rename")
            with (
                patch.object(fast_gate, "ROOT", root),
                patch.object(fast_gate, "merge_base", return_value=base),
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                self.assertEqual(fast_gate.main(Usage({"list": "true"})), 0)
        listed = out.getvalue().splitlines()
        self.assertIn("tests.test_app", listed)
        self.assertIn("tests.test_reads", listed)
        self.assertNotIn("tests.test_unrelated", listed)

    def test_fuzz_modules_are_test_labels_but_fixture_modules_are_dependencies(self):
        self.assertTrue(fast_gate.is_test(Path("tests/fuzz/api_properties.py")))
        self.assertFalse(fast_gate.is_test(Path("tests/fixtures/example_hq_plugin/views.py")))
        self.assertFalse(fast_gate.is_test(Path("tests/fuzz/__init__.py")))


# What a failing `mise run -c` prints at its end, as it prints it.
FAILING = r"""
import sys
sys.stdout.write("..F")
sys.stdout.flush()
print("\nFinished in 1.0s")
print("ERROR 2 task(s) failed:")
print("[checks] exited with status 1")
print("[lint:ruff] exited with status 1")
sys.exit(1)
"""


# A failure and an error, for the runner to report as it reports them.
RUNNER_FAILURES = """
import unittest


def missing():
    raise LookupError("no example.invalid record")


class Case(unittest.TestCase):
    def test_compares(self):
        self.assertEqual("expected", "found")

    def test_raises(self):
        missing()
"""


# `python -m tools.gate.verdict`, keeping its verdict in the folder named first.
VERDICT_IN = (
    "import sys; from pathlib import Path; from tools.gate import verdict;"
    " verdict.VERDICTS = Path(sys.argv[1]); sys.exit(verdict.main(sys.argv[2:]))"
)


@tag("source", "tree")
class GateVerdictTests(GateTestCase):
    def gate(self, *cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), "check", *cmd],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_last_line_names_the_tasks_mise_says_failed_after_the_output_unchanged(self):
        result = self.gate(sys.executable, "-c", FAILING)
        self.assertEqual(result.returncode, 1)
        *output, last = result.stdout.splitlines()
        self.assertIn("..F", output)
        self.assertIn("[lint:ruff] exited with status 1", output)
        self.assertTrue(last.startswith("[check] FAILED"), last)
        self.assertIn("checks; lint:ruff", last)

    def test_the_verdict_is_kept_whole_in_the_file_it_names(self):
        tests = [f"hq.x.tests.test_y.Case.test_{n}" for n in range(gate_verdict.SHOWN + 2)]
        failed = gate_verdict.Step("tests", ok=False, tasks=["lint:ruff"], tests=[FailedTest(test) for test in tests])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            gate_verdict.Gate("test at main").close([failed])
        *_, named, last = out.getvalue().splitlines()
        path = Path(named.rsplit(" ", 1)[-1])
        self.assertEqual(path.parent, gate_verdict.VERDICTS)
        line, *every = path.read_text().splitlines()
        self.assertEqual(line, last)
        self.assertIn("and 2 more", last)
        self.assertEqual(every, ["lint:ruff", *tests])

    def test_the_last_line_names_the_tests_the_runner_says_failed_and_the_file_says_why(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "test_example.py").write_text(RUNNER_FAILURES)
            result = self.gate(sys.executable, "-c", UNITTEST, "discover", "-s", folder, "-t", folder)
        self.assertEqual(result.returncode, 1)
        last = result.stdout.splitlines()[-1]
        self.assertIn("tests: test_example.Case.test_compares, test_example.Case.test_raises", last)
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text().splitlines()
        self.assertIn("test_example.Case.test_compares: AssertionError: 'expected' != 'found'", kept)
        self.assertIn("test_example.Case.test_raises: LookupError: no example.invalid record", kept)
        traceback = kept[kept.index("test_example.Case.test_raises:") :]
        self.assertTrue(any("missing()" in line for line in traceback), traceback)

    def test_a_pass_says_passed(self):
        result = self.gate(sys.executable, "-c", "print('[lint:ruff] exited with status 1')")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("[check] passed in"))

    def test_a_nested_mise_run_keeps_each_passed_tasks_time(self):
        """Inside a task mise turns its timings off; the verdict turns them back on and keeps them.

        Colored, as mise writes on a CI runner even into a pipe."""
        (ROOT / "var").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as config:
            Path(config, "mise.toml").write_text(
                '[tasks."gate:one"]\nrun = "true"\n[tasks."gate:two"]\nrun = "exit 3"\n'
            )
            mise = [shutil.which("mise") or "mise", "-C", config, "run", "-c", "gate:*"]
            env = {
                **os.environ,
                **trusted_mise(config, Path(config, "state")),
                "MISE_TASK_TIMINGS": "0",
                "CLICOLOR_FORCE": "1",
            }
            result = subprocess.run(
                [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), "check", *mise],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout)
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text().splitlines()
        timed = kept[kept.index("Slowest first:") + 1 :]
        self.assertEqual([line.split()[-1] for line in timed], ["gate:one"])
        self.assertIn("gate:two", kept[: kept.index("Slowest first:")])

    def test_each_time_is_read_in_its_unit_and_kept_slowest_first(self):
        step = gate_verdict.Step("checks")
        for line in ("[a] Finished in 9.3ms", "[b] Finished in 61.01s", "[c] Finished in 12µs", "[d] Finished in 1.5s"):
            step.read(f"[checks] {line}")
        self.assertEqual(
            {task: round(seconds, 6) for task, seconds in step.took.items()},
            {"a": 0.0093, "b": 61.01, "c": 0.000012, "d": 1.5},
        )
        ranked = [line.split()[-1] for line in gate_verdict.timings([step])[2:]]
        self.assertEqual(ranked, ["b", "d", "a", "c"])

    def test_steps_run_in_turn_and_a_failure_without_a_task_is_named_by_its_label(self):
        result = self.gate(
            "--step", "Python one", sys.executable, "-c", "pass",
            "--step", "Python two", sys.executable, "-c", "raise SystemExit(1)",
        )  # fmt: skip
        self.assertEqual(result.returncode, 1)
        self.assertIn("[check] Python one", result.stdout.splitlines())
        self.assertIn(": Python two; rerun: ", result.stdout.splitlines()[-1])
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text()
        self.assertIn("Python one", kept[kept.index("Slowest first:") :])

    def test_a_step_without_its_command_is_refused(self):
        result = self.gate("--step", "Python one")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr)

    def test_fast_names_each_failed_step_and_keeps_its_pass_line(self):
        ruff = gate_verdict.Step("tasks", ok=False, tasks=["lint:ruff"])
        tests = gate_verdict.Step("tests", ok=False, tests=[FailedTest("hq.x.tests.test_y.Case.test_z")])
        integrity = gate_verdict.Step("patch integrity", ok=False)
        for steps, expected in (
            ([ruff, tests, integrity], ": lint:ruff; tests: hq.x.tests.test_y.Case.test_z; patch integrity"),
            ([gate_verdict.Step("tests")], "passed in"),
        ):
            with self.subTest(expected=expected):
                code, printed = run_main(selected=("lint:ruff",), step=[*steps, *[gate_verdict.Step("more")] * 3])
                self.assertEqual(code, 0 if expected == "passed in" else 1)
                self.assertIn(expected, printed[-1])


@tag("source", "tree")
class FastGateTurnTests(GateTestCase):
    """fast holds the machine's heavy turn for its test run alone, so a worktree's quick gates never queue,
    and a quick gate that takes the turn itself runs after the tests, never beside them."""

    def test_only_the_tests_and_the_gates_that_take_it_wait_for_the_heavy_turn(self):
        quick = (*fast_gate.TASKS, fast_gate.SHELL)
        taking = {task for task in quick if takes_the_turn(mise_tasks()[task])}
        self.assertTrue(taking)
        recorder = Recorder()
        run_main(("hq/example.py",), quick, recorder, affected=(set(), set()), test_labels=([], 0))

        labels = [label for label, _ in recorder.calls]
        tests = labels.index("tests")
        before = {word for _, cmd in recorder.calls[:tests] for word in cmd}
        after = {word for _, cmd in recorder.calls[tests:] for word in cmd}
        self.assertFalse(before & taking)
        self.assertLessEqual(taking, after)
        self.assertLessEqual(set(quick), before | after)
        for label, cmd in recorder.calls:
            with self.subTest(step=label):
                self.assertEqual(" ".join(IN_TURN) in " ".join(cmd), label == "tests")
        self.assertFalse(takes_the_turn(mise_tasks()["fast"]))


@tag("source", "tree")
class FastGateExtensionSuiteTests(GateTestCase):
    """A shared UI change runs the extensions' suites with a set named, and says it skipped them without one."""

    def run_fast(self, plugins: str | None) -> tuple[list[str], str]:
        recorder = Recorder()
        environ = {key: value for key, value in os.environ.items() if key != "HQ_LOCAL_PLUGINS"}
        if plugins:
            environ["HQ_LOCAL_PLUGINS"] = plugins
        with patch.dict(os.environ, environ, clear=True):
            _, printed = run_main(("templates/example.html",), step=recorder)
        return [label for label, _ in recorder.calls], "\n".join(printed)

    def test_a_named_set_runs_the_extension_suites(self):
        labels, out = self.run_fast("example_plugin.plugin:ExamplePlugin")
        self.assertTrue(any("extension suites" in label for label in labels))
        self.assertNotIn("skipped", out)

    def test_without_a_set_the_skip_is_said_with_the_command_that_names_one(self):
        labels, out = self.run_fast(None)
        self.assertFalse(any("extension suites" in label for label in labels))
        said = [line for line in out.splitlines() if "skipped" in line]
        self.assertEqual(len(said), 1)
        self.assertIn("mise run setup:extensions", said[0])


# Each side opens the pipe the other holds: opening a FIFO waits until both
# ends are open, so neither finishes unless the two run at once. The alarm
# bounds a run where they never meet.
MEETS = """
import signal, sys
signal.alarm(20)
side, pipe, exit_code = sys.argv[1:]
print(f"{side} starts", flush=True)
if side == "tests":
    with open(pipe, "w") as end:
        end.write("met")
else:
    with open(pipe) as end:
        end.read()
print(f"{side} ends", flush=True)
sys.exit(int(exit_code))
"""


@tag("source", "tree")
class FastOverlapTests(GateTestCase):
    """fast runs its light quick gates beside the tests."""

    def run_fast(self, quick_exit: int = 0, tests_exit: int = 0) -> tuple[int, list[str]]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        script = Path(folder.name, "meet.py")
        script.write_text(MEETS)
        os.mkfifo(script.with_name("pipe"))

        def meets(side: str, exit_code: int) -> list[str]:
            return [sys.executable, str(script), side, str(script.with_name("pipe")), str(exit_code)]

        # The patch integrity step runs for real, against a base that resolves.
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        return run_main(
            selected=("example:quick",),
            merge_base=head.stdout.strip(),
            affected=(set(), set()),
            test_labels=([], 0),
            turn_takers=(),
            tasks_command=meets("quick", quick_exit),
            test_command=meets("tests", tests_exit),
        )

    def test_the_light_quick_gates_run_while_the_tests_do_each_output_whole_under_its_label(self):
        # Each side ends only once the other has opened its end.
        _, printed = self.run_fast()
        labels = set()
        for side in ("tests", "quick"):
            with self.subTest(side=side):
                starts = printed.index(f"{side} starts")
                self.assertEqual(printed[starts + 1], f"{side} ends", "\n".join(printed))
                self.assertTrue(printed[starts - 1].startswith("[fast] "), printed[starts - 1])
                labels.add(printed[starts - 1])
        self.assertEqual(len(labels), 2)

    def test_the_verdict_names_every_failed_step_and_its_rerun(self):
        code, printed = self.run_fast(quick_exit=1, tests_exit=1)
        self.assertEqual(code, 1)
        named, _, rerun = printed[-1].partition("; rerun: ")
        self.assertIn("FAILED", named)
        for side in ("tests", "quick"):
            with self.subTest(side=side):
                label = printed[printed.index(f"{side} starts") - 1].removeprefix("[fast] ")
                self.assertIn(label, named)
                self.assertIn(f"meet.py {side}", rerun)


@tag("source")
class ShellSourcesTests(GateTestCase):
    """The one definition of a shell file, asked by the shell gate and by fast, on a fixture repository."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        files = {
            "script": "#!/bin/sh\necho\n",
            "bash.sh": "#!/usr/bin/env bash\necho\n",
            "lib.sh": "# shellcheck shell=sh\nx=1\n",
            "tool.py": "#!/usr/bin/env python3\n",
            "notes.sh.md": "text\n",
            "gone.sh": "#!/bin/sh\n",
            "gone.txt": "text\n",
            "ignored.sh": "#!/bin/sh\n",
            "new.sh": "#!/bin/dash\n",
        }
        for name, text in files.items():
            (self.root / name).write_text(text)
        (self.root / ".gitignore").write_text("ignored.sh\nnew.sh\n")
        git = fixture_git(self.root)
        git("init", "--quiet")
        git("add", "-A")
        git("commit", "--quiet", "--no-verify", "-m", "base")
        self.base = git("rev-parse", "HEAD")
        (self.root / ".gitignore").write_text("ignored.sh\n")
        (self.root / "gone.sh").unlink()
        (self.root / "gone.txt").unlink()

    def lister(self, *paths: str) -> list[str]:
        done = subprocess.run(
            ["/bin/sh", str(SOURCES), *paths], cwd=self.root, check=True, capture_output=True, text=True
        )
        return done.stdout.splitlines()

    def test_without_paths_it_lists_every_shell_file_git_tracks_or_would_track(self):
        self.assertEqual(sorted(self.lister()), ["bash.sh", "lib.sh", "new.sh", "script"])

    def test_a_shell_file_whose_name_git_quotes_in_a_list_of_lines_is_listed_as_it_is(self):
        """systemd's escaping writes a backslash into a name, which git quotes unless asked for -z."""

        name = r"run\x2dname.sh"
        (self.root / name).write_text("#!/bin/sh\n")
        self.assertIn(name, self.lister())

    def test_given_paths_it_names_those_of_them_that_are_shell_files(self):
        self.assertEqual(self.lister("tool.py", "lib.sh", "missing.sh", "ignored.sh"), ["lib.sh", "ignored.sh"])

    def test_fast_reads_a_present_path_here_and_a_deleted_one_as_the_base_had_it(self):
        with patch.object(fast_gate, "ROOT", self.root):
            listed = fast_gate.shell_files(["tool.py", "script", "gone.sh", "gone.txt"], self.base)
            self.assertEqual(sorted(listed), ["gone.sh", "script"])
            self.assertNotIn("lint:shell", fast_gate.selected_tasks(["gone.txt", "tool.py"], self.base))
            self.assertIn("lint:shell", fast_gate.selected_tasks(["gone.sh"], self.base))


WRITE = """
import os, sqlite3
path = os.environ["SEVERINO_DATABASE_PATH"]
connection = sqlite3.connect(path, timeout=0)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("CREATE TABLE gate (x)")
connection.commit()
print("database=" + path)
"""


@tag("source")
class GateDatabaseTests(GateTestCase):
    def test_gates_run_side_by_side_each_write_a_database_the_next_test_run_sweeps(self):
        # Two processes making one new SQLite file WAL at once: one is refused.
        (ROOT / "var").mkdir(exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=ROOT / "var") as config,
            tempfile.TemporaryDirectory() as scratch,
        ):
            (Path(config) / "write.py").write_text(WRITE)
            run = json.dumps(f"{sys.executable} write.py")
            (Path(config) / "mise.toml").write_text(
                "".join(f'[tasks."gate:{n}"]\nextends = "django"\nrun = {run}\n' for n in range(4))
            )
            result = subprocess.run(
                [shutil.which("mise") or "mise", "run", "-c", "--jobs", "4", "gate:*"],
                cwd=config,
                env={**os.environ, **trusted_mise(config, Path(config, "state")), "TMPDIR": scratch},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            paths = re.findall(r"database=(\S+)", result.stdout)
            self.assertEqual(len(set(paths)), 4)
            self.assertTrue(all(Path(path).is_relative_to(scratch) for path in paths))
            sweep_abandoned(Path(scratch), os.getpid())
            self.assertFalse([path for path in Path(scratch).iterdir() if path.name.startswith(PREFIX)])
