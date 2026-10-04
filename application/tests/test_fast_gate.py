"""The inner-loop gate maps a change to the tests that reach it."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
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
        reached = fast_gate.imports(Path("application/tests/test_domains.py"), modules)
        self.assertIn("application.domains", reached)

    def test_a_declarations_string_reference_is_not_an_import(self):
        modules = {fast_gate.module_name(p) for p in self.files}
        reached = fast_gate.imports(Path("application/domains.py"), modules)
        self.assertNotIn("expenses.urls", reached)

    def test_mypy_is_given_only_the_typed_python_it_checks(self):
        targets = fast_gate.mypy_targets(
            ["hq_mcp/tests.py", "hq_sdk/contract.json", "application/domains.py"]
        )
        self.assertEqual(targets, ["application/domains.py"])

    def test_an_app_change_selects_the_tests_that_use_the_app(self):
        labels = self.labels("expenses/views.py")
        self.assertIn("application.tests.test_domains", labels)
        self.assertNotIn("hq_mcp.tests", labels)

    def test_a_test_change_selects_that_test_and_the_architecture_tests(self):
        labels = self.labels("core/tests/test_security.py")
        self.assertIn("core.tests.test_security", labels)
        self.assertIn("application.tests.test_architecture", labels)

    def test_a_template_belongs_to_its_app(self):
        _, touched = fast_gate.affected(["templates/expenses/list.html"], [], self.files)
        self.assertEqual(touched, {"expenses"})

    def test_apps_are_derived_from_the_tree(self):
        self.assertLessEqual({"expenses", "core", "projects"}, fast_gate.django_apps())
