"""Every real-browser test is run by a browser gate: a class no task runs never
runs, and CI's shares of a gate run all of it. The gates' `--fallback-face`
still finds the face it withholds."""

import os
import shlex
import tomllib
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import SimpleTestCase, tag
from django.test.runner import DiscoverRunner

from hq.platform.application.tests.facts import grouped, scan
from hq.platform.core.test_runner import in_this_share, tests_in
from hq.platform.core.tests.browser import shards
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


def engine(task: dict) -> str:
    """The engine a browser task draws in, as `BrowserGate` reads it from the environment."""

    return dict(entry.split("=", 1) for entry in task["env"] if "=" in entry).get("HQ_BROWSER_ENGINE", "webkit")


def gate_tests(task: dict) -> list[unittest.TestCase]:
    """The tests a Django browser task runs, loaded as its ``manage.py test`` loads them."""

    words = shlex.split(body(task))
    labels = [word for word in words if word.startswith("hq.")]
    pattern = words[words.index("--pattern") + 1] if "--pattern" in words else None
    return list(tests_in(DiscoverRunner(pattern=pattern, verbosity=0).build_suite(labels)))


def django_browser_tasks() -> dict[str, dict]:
    return {name: task for name, task in browser_tasks().items() if "manage.py test" in body(task)}


@tag("source", "tree")
class BrowserGateTaskTests(SimpleTestCase):
    def test_every_browser_test_class_runs_in_webkit_and_another_engine_runs_only_what_webkit_runs(self):
        classes = set(gate_classes())
        self.assertTrue(classes)
        ran = {name: {test.id() for test in gate_tests(task)} for name, task in django_browser_tasks().items()}
        webkit = set().union(*(ids for name, ids in ran.items() if engine(browser_tasks()[name]) == "webkit"))
        run_classes = {test_id.rsplit(".", 1)[0] for test_id in webkit}
        self.assertEqual(
            sorted(classes - run_classes), [], "no WebKit browser task runs these: see mise-tasks/browser.toml"
        )
        for name, ids in ran.items():
            if engine(browser_tasks()[name]) != "webkit":
                with self.subTest(task=name):
                    self.assertTrue(ids, "a leg in another engine names the tests it runs")
                    self.assertEqual(sorted(ids - webkit), [], "no WebKit leg runs these")

    def test_the_shares_of_a_split_gate_run_the_whole_gate_each_page_and_test_once(self):
        """However many shares CI splits a gate into, each test that is not a
        sweep runs in exactly one, and a sweep in all, each reading a share of
        its pages that no other reads, together every page."""

        for name, task in django_browser_tasks().items():
            tests = gate_tests(task)
            for total in range(1, 13):
                with self.subTest(task=name, shares=total):
                    shares = []
                    for index in range(1, total + 1):
                        with mock.patch.dict(os.environ, {shards.VARIABLE: f"{index}/{total}"}):
                            shares.append({test.id() for test in tests if in_this_share(test)})
                    sweeping = {test.id() for test in tests if shards.sweeps(test)}
                    counted = Counter(test_id for share in shares for test_id in share)
                    self.assertEqual(set(counted), {test.id() for test in tests})
                    self.assertEqual(
                        {test_id for test_id, count in counted.items() if count != 1}, sweeping if total > 1 else set()
                    )
                    self.assertTrue(all(counted[test_id] == total for test_id in sweeping))
                    for test_id in sweeping:
                        pages = [shards.share(range(50), test_id, (index, total)) for index in range(1, total + 1)]
                        self.assertEqual(sorted(page for share in pages for page in share), list(range(50)))

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
