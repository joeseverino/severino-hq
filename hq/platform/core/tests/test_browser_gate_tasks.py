"""Every real-browser test is run by a browser gate: a class no task names never runs."""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
MODULES = ("hq.platform.core.browser_tests", "hq.platform.core.estate_browser_tests")
BASE = "BrowserGate"


def gate_classes(root: Path) -> dict[str, str]:
    """Each test class built on the browser gate, as ``module.Class``, by module."""

    found: dict[str, str] = {}
    gates = {BASE}
    trees = {module: ast.parse((root / (module.replace(".", "/") + ".py")).read_text()) for module in MODULES}
    grew = True
    while grew:
        grew = False
        for module, tree in trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or node.name in gates:
                    continue
                if any(isinstance(base, ast.Name) and base.id in gates for base in node.bases):
                    gates.add(node.name)
                    found[f"{module}.{node.name}"] = module
                    grew = True
    return found


def browser_labels() -> set[str]:
    """Every word the ``browser:*`` tasks run."""

    return {word for name, task in mise_tasks().items() if name.startswith("browser:") for word in body(task).split()}


@tag("source")
class BrowserGateTaskTests(SimpleTestCase):
    def test_every_browser_test_class_is_named_by_a_browser_task(self):
        classes = gate_classes(ROOT)
        labels = browser_labels()

        self.assertTrue(classes)
        unnamed = sorted(name for name, module in classes.items() if name not in labels and module not in labels)
        self.assertEqual(unnamed, [], "no browser:* task runs these")
