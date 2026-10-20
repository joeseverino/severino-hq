"""The inner-loop gate maps a change to the tests that reach it."""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, tag

from hq.config.sandbox import PREFIX, sweep_abandoned

ROOT = Path(__file__).resolve().parents[4]


def load(name: str):
    """A script as a module; fast_gate imports gate_verdict by the name it has beside it."""

    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate_verdict = load("gate_verdict")
fast_gate = load("fast_gate")


@tag("source")
class FastGateMappingTests(SimpleTestCase):
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
class FastGateBaseTests(SimpleTestCase):
    REFS = {"main": "aaa", "topic": "bbb"}

    def resolve(self, base, check_base=""):
        def git(*args):
            if args[-1] not in self.REFS:
                raise subprocess.CalledProcessError(128, args)
            return [self.REFS[args[-1]]]

        with patch.object(fast_gate, "git", git), patch.dict(os.environ, {"CHECK_BASE": check_base}):
            return fast_gate.merge_base(base)

    def test_a_base_asked_for_is_the_one_used(self):
        self.assertEqual(self.resolve("topic"), "bbb")
        self.assertEqual(self.resolve(None, check_base="topic"), "bbb")

    def test_a_base_asked_for_that_does_not_resolve_fails(self):
        for base, check_base in (("typo", ""), (None, "typo"), ("typo", "topic")):
            with self.subTest(base=base, check_base=check_base), self.assertRaises(SystemExit) as raised:
                self.resolve(base, check_base)
            self.assertIn("typo", str(raised.exception.code))

    def test_with_none_asked_for_a_missing_origin_falls_back_to_main(self):
        self.assertEqual(self.resolve(None), "aaa")


@tag("source")
class FastGateNestedLayoutTests(SimpleTestCase):
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


# What a failing `mise run -c` and a failing test run print, as they print it.
FAILING = r"""
import sys
sys.stdout.write("..F")
sys.stdout.flush()
sys.stderr.write("\nFAIL: test_z (hq.x.tests.test_y.Case.test_z)\n")
sys.stderr.write("ERROR: setUpClass (hq.x.tests.test_w.Other)\n")
print("[checks] [checks:ruff] exited with status 1")
sys.exit(1)
"""


@tag("source")
class GateVerdictTests(SimpleTestCase):
    def gate(self, *cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gate_verdict.py"), "check", *cmd],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_last_line_names_the_failed_task_and_tests_after_the_output_unchanged(self):
        result = self.gate(sys.executable, "-c", FAILING)
        self.assertEqual(result.returncode, 1)
        *output, last = result.stdout.splitlines()
        self.assertIn("..F", output)
        self.assertIn("FAIL: test_z (hq.x.tests.test_y.Case.test_z)", output)
        self.assertTrue(last.startswith("[check] FAILED"), last)
        for named in ("checks:ruff", "hq.x.tests.test_y.Case.test_z", "hq.x.tests.test_w.Other.setUpClass"):
            self.assertIn(named, last)

    def test_a_pass_says_passed(self):
        result = self.gate(sys.executable, "-c", "print('FAIL: test_z (a.B.test_z)')")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("[check] passed in"))

    def test_fast_names_each_failed_step_and_keeps_its_pass_line(self):
        ruff = gate_verdict.Step("tasks", ok=False, tasks=["checks:ruff"])
        tests = gate_verdict.Step("tests", ok=False, tests=["hq.x.tests.test_y.Case.test_z"])
        integrity = gate_verdict.Step("patch integrity", ok=False)
        for steps, expected in (
            ([ruff, tests, integrity], ": checks:ruff; tests: hq.x.tests.test_y.Case.test_z; patch integrity"),
            ([gate_verdict.Step("tests")], "passed in"),
        ):
            with (
                self.subTest(expected=expected),
                patch.object(fast_gate, "merge_base", return_value="0" * 40),
                patch.object(fast_gate, "changed_files", return_value=([], [])),
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
class GateDatabaseTests(SimpleTestCase):
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
                "".join(
                    f'[tasks."gate:{n}"]\nextends = "{template}"\nrun = {run}\n'
                    for n, template in enumerate(("django", "django", "composed", "composed"))
                )
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
