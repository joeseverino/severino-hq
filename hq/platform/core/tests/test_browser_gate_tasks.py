"""Every real-browser test is run by a browser gate: a class no task names never runs."""

import ast
import tomllib
from pathlib import Path

from django.test import SimpleTestCase, tag

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


def browser_labels(root: Path) -> set[str]:
    """Every word the ``browser:*`` tasks run."""

    tasks = tomllib.loads((root / "mise.toml").read_text())["tasks"]
    return {
        word
        for name, task in tasks.items()
        if name.startswith("browser:")
        for word in " ".join(task["run"] if isinstance(task.get("run"), list) else [task.get("run", "")]).split()
    }


@tag("source")
class BrowserGateTaskTests(SimpleTestCase):
    def test_every_browser_test_class_is_named_by_a_browser_task(self):
        classes = gate_classes(ROOT)
        labels = browser_labels(ROOT)

        self.assertTrue(classes)
        unnamed = sorted(name for name, module in classes.items() if name not in labels and module not in labels)
        self.assertEqual(unnamed, [], "no browser:* task runs these")
