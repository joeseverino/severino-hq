"""The inner-loop gate maps a change to the tests that reach it."""

from __future__ import annotations

import importlib.util
import tempfile
from unittest.mock import patch
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("fast_gate", ROOT / "scripts" / "fast_gate.py")
assert spec and spec.loader
fast_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fast_gate)


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

    def test_mypy_is_given_only_the_typed_python_it_checks(self):
        targets = fast_gate.mypy_targets(
            ["hq/platform/mcp/tests.py", "hq_sdk/contract.json", "hq/platform/application/domains.py"]
        )
        self.assertEqual(targets, ["hq/platform/application/domains.py"])

    def test_an_app_change_selects_the_tests_that_use_the_app(self):
        labels = self.labels("hq/domains/expenses/views.py")
        self.assertIn("hq.platform.application.tests.test_domains", labels)
        self.assertNotIn("hq.platform.mcp.tests", labels)

    def test_a_test_change_selects_that_test_and_the_architecture_tests(self):
        labels = self.labels("hq/platform/core/tests/test_security.py")
        self.assertIn("hq.platform.core.tests.test_security", labels)
        self.assertIn("hq.platform.application.tests.test_architecture", labels)

    def test_a_template_belongs_to_its_app(self):
        _, touched = fast_gate.affected(["templates/expenses/list.html"], [], self.files)
        self.assertEqual(touched, {"expenses"})

    def test_apps_are_derived_from_the_tree(self):
        self.assertLessEqual({"expenses", "core", "projects"}, set(fast_gate.django_apps().values()))


class FastGateConfigTests(SimpleTestCase):
    def test_toml_configuration_bounds_typed_seams_and_excludes_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                '[tool.mypy]\nfiles = ["hq_sdk", "hq/platform/application/security.py"]\n'
                'exclude = "(^|/)(test_[^/]*|tests)/"\n'
            )
            with patch.object(fast_gate, "ROOT", root):
                self.assertEqual(
                    fast_gate.mypy_targets(["hq_sdk/capabilities.py", "hq_sdk/tests/test_contract.py", "hq/platform/application/security.py", "application/untyped.py"]),
                    ["hq_sdk/capabilities.py", "hq/platform/application/security.py"],
                )


class FastGateNestedLayoutTests(SimpleTestCase):
    def test_app_config_label_can_differ_from_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path("hq/domains/example/apps.py")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text('class ExampleConfig:\n    name = "hq.domains.example"\n    label = "stable_label"\n')
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
            with patch.object(fast_gate, "ROOT", root), patch.object(fast_gate, "git", return_value=[str(path), "example/views.py"]):
                self.assertEqual(fast_gate.python_files(), [path])

    def test_fuzz_modules_are_test_labels_but_fixture_modules_are_dependencies(self):
        self.assertTrue(fast_gate.is_test(Path("tests/fuzz/api_properties.py")))
        self.assertFalse(fast_gate.is_test(Path("tests/fixtures/example_hq_plugin/views.py")))
        self.assertFalse(fast_gate.is_test(Path("tests/fuzz/__init__.py")))
