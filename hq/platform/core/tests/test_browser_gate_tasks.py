"""Every real-browser test is run by a browser gate: a class no task names never
runs. The gates' `--fallback-face` still finds the face it withholds."""

import os
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.facts import grouped, scan
from hq.platform.core.tests.browser.serving import in_the_face
from hq.platform.core.tests.browser_modules import browser_modules
from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
BASE = "BrowserGate"


# A class, with its name captured, so a match inside it says which class it is in.
CLASS: dict[str, Any] = {"kind": "class_definition", "has": {"field": "name", "pattern": "$CLASS"}}
CLASS_FACTS: dict[str, dict[str, Any]] = {
    "class": {"kind": "identifier", "inside": {"kind": "class_definition", "field": "name"}},
    # A base named plainly, as the gate's own classes are.
    "base": {"kind": "identifier", "inside": {"kind": "argument_list", "inside": {**CLASS, "field": "superclasses"}}},
    # A test method of the class itself, decorated or not.
    "test": {
        "kind": "identifier",
        "regex": "^test_",
        "inside": {
            "kind": "function_definition",
            "field": "name",
            "any": [
                {"inside": {"kind": "block", "inside": CLASS}},
                {"inside": {"kind": "decorated_definition", "inside": {"kind": "block", "inside": CLASS}}},
            ],
        },
    },
}


def gate_classes() -> dict[str, str]:
    """Each class built on the browser gate that holds tests, its own or inherited, as ``module.Class``, by module.

    A class that holds only rules for others to run runs nothing when named.
    """

    modules = {path: module for module, path in browser_modules().items()}
    found = grouped(scan(CLASS_FACTS, *(path.relative_to(ROOT) for path in modules)))
    defined = {match.text: modules[match.path] for match in found["class"]}
    named: dict[str, list[str]] = {}
    for match in found.get("base", []):
        named.setdefault(match.captured["CLASS"], []).append(match.text)
    own = {match.captured["CLASS"] for match in found.get("test", [])}

    def bases(name: str) -> list[str]:
        return [base for base in named.get(name, []) if base in defined]

    def on_the_gate(name: str) -> bool:
        return name == BASE or any(on_the_gate(base) for base in bases(name))

    def holds_tests(name: str) -> bool:
        return name in own or any(holds_tests(base) for base in bases(name))

    return {
        f"{module}.{name}": module
        for name, module in defined.items()
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
        self.assertEqual(
            unnamed, [], "no browser:* task runs these: name each in one leg's vars.classes in mise-tasks/browser.toml"
        )
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
        """A template extends none, so the browser gate template carries the
        env of ``django`` itself: a gate never
        opens the developer's database."""

        templates = tomllib.loads((ROOT / "mise.toml").read_text())["task_templates"]
        django = templates["django"]["env"]
        self.assertEqual({key: templates["django-browser"]["env"].get(key) for key in django}, django)


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
