"""Every real-browser test is run by a browser gate: a class no task names never
runs. The gates' `--fallback-face` still finds the face it withholds."""

import ast
import os
import tomllib
from collections import Counter
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.browser.serving import in_the_face
from hq.platform.core.tests.browser_modules import browser_modules
from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
BASE = "BrowserGate"


def gate_classes() -> dict[str, str]:
    """Each class built on the browser gate that holds tests, its own or inherited, as ``module.Class``, by module.

    A class that holds only rules for others to run runs nothing when named.
    """

    defined: dict[str, tuple[str, ast.ClassDef]] = {}
    for module, path in browser_modules().items():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                defined[node.name] = (module, node)

    def bases(node: ast.ClassDef) -> list[str]:
        return [base.id for base in node.bases if isinstance(base, ast.Name) and base.id in defined]

    def on_the_gate(name: str) -> bool:
        return name == BASE or any(on_the_gate(base) for base in bases(defined[name][1]))

    def holds_tests(name: str) -> bool:
        node = defined[name][1]
        own = any(isinstance(item, ast.FunctionDef) and item.name.startswith("test_") for item in node.body)
        return own or any(holds_tests(base) for base in bases(node))

    return {
        f"{module}.{name}": module
        for name, (module, _node) in defined.items()
        if name != BASE and on_the_gate(name) and holds_tests(name)
    }


def browser_tasks() -> dict[str, dict]:
    return {name: task for name, task in mise_tasks().items() if name.startswith("browser:")}


def browser_labels() -> set[str]:
    """Every word the ``browser:*`` tasks run."""

    return {word for task in browser_tasks().values() for word in body(task).split()}


@tag("source", "tree")
class BrowserGateTaskTests(SimpleTestCase):
    def test_every_browser_test_class_is_named_by_a_browser_task(self):
        classes = gate_classes()
        labels = browser_labels()

        self.assertTrue(classes)
        unnamed = sorted(name for name, module in classes.items() if name not in labels and module not in labels)
        self.assertEqual(unnamed, [], "no browser:* task runs these")
        # Once per engine: a WebKit leg names its classes, and a leg in
        # another engine runs a WebKit leg rather than naming any.
        named = Counter(
            name
            for task in browser_tasks().values()
            for name, module in classes.items()
            if {name, module} & set(body(task).split())
        )
        self.assertEqual(sorted(name for name, count in named.items() if count > 1), [], "two browser tasks run these")
        run = Counter(
            word for name, task in browser_tasks().items() if "chromium" in name for word in body(task).split()
        )
        legs = [name for name in browser_tasks() if name.startswith("browser:layout-") and "chromium" not in name]
        self.assertEqual({leg: run[leg] for leg in legs}, dict.fromkeys(legs, 1), "Chromium runs each layout leg once")

    def test_a_browser_gate_s_template_restates_django_s_env(self):
        """A template extends none, so each Django gate template carries the
        env of ``django`` itself: a gate never opens the developer's database."""

        templates = tomllib.loads((ROOT / "mise.toml").read_text())["task_templates"]
        for name in ("django-heavy", "django-browser"):
            with self.subTest(template=name):
                self.assertEqual(templates[name]["env"], templates["django"]["env"])


@tag("source")
class FallbackFaceTests(SimpleTestCase):
    """Without the face the stylesheet names, `--fallback-face` would quietly
    draw every page in Inter."""

    def served(self, face):
        css = (ROOT / "static" / "css" / "app" / "tokens.css").read_bytes()
        with mock.patch.dict(os.environ, {"HQ_BROWSER_FACE": face}):
            return in_the_face(css)

    def test_the_fallback_face_withholds_inter_so_the_stack_falls_through(self):
        served = self.served("fallback")
        self.assertNotIn(b"@font-face", served)
        self.assertIn(b"--font:", served)

    def test_the_gates_otherwise_wait_for_inter(self):
        served = self.served("")
        self.assertIn(b"inter-variable", served)
        self.assertNotIn(b"font-display: optional", served)

    def test_an_unknown_face_is_refused(self):
        with self.assertRaisesMessage(ValueError, "HQ_BROWSER_FACE"):
            self.served("serif")
