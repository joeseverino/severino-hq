"""The inner-loop gate maps a change to the tests that reach it."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from unittest.mock import patch

from django.test import tag

from hq.config.sandbox import PREFIX, sweep_abandoned
from tests.mise_tasks import mise_tasks
from tools.gate import fast as fast_gate, verdict as gate_verdict
from tools.gate.tests.base import UNITTEST, GateTestCase
from tools.gate.verdict import FailedTest

ROOT = Path(__file__).resolve().parents[3]


@tag("source")
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
        modules = {fast_gate.module_name(p) for p in self.files}
        reached = fast_gate.imports(Path("hq/platform/application/tests/test_domains.py"), modules)
        self.assertIn("hq.platform.application.domains", reached)

    def test_a_declarations_string_reference_is_not_an_import(self):
        modules = {fast_gate.module_name(p) for p in self.files}
        reached = fast_gate.imports(Path("hq/platform/application/domains.py"), modules)
        self.assertNotIn("hq.domains.expenses.urls", reached)

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
        """A task moved between mise.toml, tasks/ and mise-tasks/ is read through tests.mise_tasks, by no path."""
        for changed in ("mise.toml", "tasks/browser.toml", "mise-tasks/suite/as-image"):
            with self.subTest(changed=changed):
                self.assertIn("hq.platform.core.tests.test_browser_gate_tasks", self.labels(changed))
        self.assertNotIn("hq.platform.core.tests.test_browser_gate_tasks", self.labels("docs/Development.md"))

    def test_a_test_change_selects_that_test_and_the_architecture_tests(self):
        labels = self.labels("hq/platform/core/tests/test_security.py")
        self.assertIn("hq.platform.core.tests.test_security", labels)
        self.assertIn("hq.platform.application.tests.test_architecture", labels)

    def test_a_template_belongs_to_its_app(self):
        _, touched = fast_gate.affected(["templates/expenses/list.html"], [], self.files)
        self.assertEqual(touched, {"expenses"})

    def test_apps_are_derived_from_the_tree(self):
        self.assertLessEqual({"expenses", "core", "projects"}, set(fast_gate.django_apps().values()))


@tag("source")
class FastGateTaskSelectionTests(GateTestCase):
    def test_every_quick_gate_declares_paths_that_name_files_of_this_tree(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True)
        files = tracked.stdout.splitlines()
        for task, patterns in fast_gate.TASKS.items():
            with self.subTest(task=task):
                self.assertTrue(patterns, f"{task} declares no paths, so fast would never run it")
                for pattern in patterns:
                    self.assertTrue(any(fnmatch(path, pattern) for path in files), f"{task}: {pattern} names no file")

    def test_the_quick_gates_are_tasks_and_every_drift_gate_is_one(self):
        tasks = mise_tasks()
        self.assertLessEqual(set(fast_gate.TASKS), set(tasks))
        self.assertLessEqual({name for name in tasks if name.startswith("drift:")}, set(fast_gate.TASKS))

    def test_a_docs_change_runs_only_the_gate_that_derives_it(self):
        self.assertEqual(fast_gate.selected_tasks(["docs/AI.md"]), ())
        self.assertEqual(fast_gate.selected_tasks(["docs/Connections.md"]), ("drift:connections-reference",))

    def test_a_python_change_runs_the_python_gates_and_not_the_stylesheet_ones(self):
        selected = fast_gate.selected_tasks(["hq/domains/docs_index/importer.py"])
        self.assertLessEqual({"checks:ruff", "checks:mypy", "tests:migrations", "drift:api-document"}, set(selected))
        self.assertNotIn("checks:css", selected)
        self.assertNotIn("drift:font", selected)

    def test_a_change_to_what_every_gate_reads_runs_them_all(self):
        for changed in ("uv.lock", "tasks/checks.toml"):
            with self.subTest(changed=changed):
                self.assertEqual(fast_gate.selected_tasks([changed]), tuple(fast_gate.TASKS))


@tag("source")
class FastGateBaseTests(GateTestCase):
    REFS = {"main": "aaa", "topic": "bbb"}

    def resolve(self, base):
        def git(*args):
            if args[-1] not in self.REFS:
                raise subprocess.CalledProcessError(128, args)
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

    def test_moved_untracked_sources_are_included_and_deleted_sources_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path("hq/domains/example/views.py")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text("")
            with (
                patch.object(fast_gate, "ROOT", root),
                patch.object(fast_gate, "git", return_value=[str(path), "example/views.py"]),
            ):
                self.assertEqual(fast_gate.python_files(), [path])

    def test_fuzz_modules_are_test_labels_but_fixture_modules_are_dependencies(self):
        self.assertTrue(fast_gate.is_test(Path("tests/fuzz/api_properties.py")))
        self.assertFalse(fast_gate.is_test(Path("tests/fixtures/example_hq_plugin/views.py")))
        self.assertFalse(fast_gate.is_test(Path("tests/fuzz/__init__.py")))

    def test_an_atheris_harness_is_not_a_test_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fuzz = root / "tests" / "fuzz"
            fuzz.mkdir(parents=True)
            for package in (root / "tests", fuzz):
                (package / "__init__.py").write_text("")
            (fuzz / "harness.py").write_text("import atheris\n")
            (fuzz / "properties.py").write_text("from hypothesis import given\n")
            with patch.object(fast_gate, "ROOT", root):
                self.assertFalse(fast_gate.is_test(Path("tests/fuzz/harness.py")))
                self.assertTrue(fast_gate.is_test(Path("tests/fuzz/properties.py")))


# What a failing `mise run -c` prints at its end, as it prints it.
FAILING = r"""
import sys
sys.stdout.write("..F")
sys.stdout.flush()
print("\nFinished in 1.0s")
print("ERROR 2 task(s) failed:")
print("[checks] exited with status 1")
print("[checks:ruff] exited with status 1")
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


@tag("source")
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
        self.assertIn("[checks:ruff] exited with status 1", output)
        self.assertTrue(last.startswith("[check] FAILED"), last)
        self.assertIn("checks; checks:ruff", last)

    def test_the_verdict_is_kept_whole_in_the_file_it_names(self):
        tests = [f"hq.x.tests.test_y.Case.test_{n}" for n in range(gate_verdict.SHOWN + 2)]
        failed = gate_verdict.Step("tests", ok=False, tasks=["checks:ruff"], tests=[FailedTest(test) for test in tests])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            gate_verdict.close("test at main", 0.0, [failed])
        *_, named, last = out.getvalue().splitlines()
        path = Path(named.rsplit(" ", 1)[-1])
        self.assertEqual(path.parent, gate_verdict.VERDICTS)
        line, *every = path.read_text().splitlines()
        self.assertEqual(line, last)
        self.assertIn("and 2 more", last)
        self.assertEqual(every, ["checks:ruff", *tests])

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
        result = self.gate(sys.executable, "-c", "print('[checks:ruff] exited with status 1')")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("[check] passed in"))

    def test_a_nested_mise_run_keeps_each_passed_tasks_time(self):
        """Inside a task mise turns its timings off; the verdict turns them back on and keeps them."""
        (ROOT / "var").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as config:
            Path(config, "mise.toml").write_text(
                '[tasks."gate:one"]\nrun = "true"\n[tasks."gate:two"]\nrun = "exit 3"\n'
            )
            mise = [shutil.which("mise") or "mise", "-C", config, "run", "-c", "gate:*"]
            result = subprocess.run(
                [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), "check", *mise],
                cwd=ROOT,
                env={**os.environ, "MISE_TRUSTED_CONFIG_PATHS": config, "MISE_TASK_TIMINGS": "0"},
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
        self.assertTrue(result.stdout.splitlines()[-1].endswith(": Python two"), result.stdout)
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text()
        self.assertIn("Python one", kept[kept.index("Slowest first:") :])

    def test_a_step_without_its_command_is_refused(self):
        result = self.gate("--step", "Python one")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr)

    def test_fast_names_each_failed_step_and_keeps_its_pass_line(self):
        ruff = gate_verdict.Step("tasks", ok=False, tasks=["checks:ruff"])
        tests = gate_verdict.Step("tests", ok=False, tests=[FailedTest("hq.x.tests.test_y.Case.test_z")])
        integrity = gate_verdict.Step("patch integrity", ok=False)
        for steps, expected in (
            ([ruff, tests, integrity], ": checks:ruff; tests: hq.x.tests.test_y.Case.test_z; patch integrity"),
            ([gate_verdict.Step("tests")], "passed in"),
        ):
            with (
                self.subTest(expected=expected),
                patch.object(fast_gate, "merge_base", return_value="0" * 40),
                patch.object(fast_gate, "changed_files", return_value=([], [])),
                patch.object(fast_gate, "selected_tasks", return_value=("checks:ruff",)),
                patch.object(fast_gate, "step", side_effect=[*steps, *[gate_verdict.Step("more")] * 3]),
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                code = fast_gate.main([])
            self.assertEqual(code, 0 if expected == "passed in" else 1)
            self.assertIn(expected, out.getvalue().splitlines()[-1])


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
                env={**os.environ, "MISE_TRUSTED_CONFIG_PATHS": config, "TMPDIR": scratch},
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
