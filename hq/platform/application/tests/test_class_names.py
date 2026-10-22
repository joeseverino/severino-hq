"""A public class name in HQ's own source means one thing.

Two top-level classes of one name in different modules make every reader ask
which one a type hint, an import or a docstring means, and a search for either
finds both. Each concept gets a name that says what it is.
"""

import ast
import tempfile
from collections import defaultdict
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import parsed, source_files

REPO_ROOT = Path(__file__).resolve().parents[4]
ROOTS = ("hq/platform/application", "hq/platform/core", "hq/domains")

# Django finds these by their names: every management command module defines a
# ``Command`` and every migration a ``Migration``, so the name is the framework's.
FRAMEWORK_NAMES = {"commands": "Command", "migrations": "Migration"}


def _defined(path: Path) -> list[str]:
    exempt = {FRAMEWORK_NAMES[part] for part in path.parts if part in FRAMEWORK_NAMES}
    return [
        node.name
        for node in parsed(path).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_") and node.name not in exempt
    ]


def collisions(root: Path, packages: tuple[str, ...]) -> dict[str, list[str]]:
    """Each public class name defined in more than one module, with where."""

    found: dict[str, list[str]] = defaultdict(list)
    for package in packages:
        for path in source_files(root / package, "*.py"):
            if "tests" in path.relative_to(root).parts:
                continue
            for name in _defined(path):
                found[name].append(str(path.relative_to(root)))
    return {name: where for name, where in found.items() if len(where) > 1}


@tag("source")
class ClassNameTests(SimpleTestCase):
    def test_a_public_class_name_is_defined_once(self):
        for package in ROOTS:
            self.assertTrue(source_files(REPO_ROOT / package, "*.py"), package)
        found = collisions(REPO_ROOT, ROOTS)
        self.assertFalse(found, "\n".join(f"{name}: {', '.join(where)}" for name, where in sorted(found.items())))

    def test_a_second_definition_is_found_and_a_framework_name_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, source in {
                "example/a.py": "class Thing: ...\nclass _Private: ...\n",
                "example/b.py": "class Thing: ...\nclass _Private: ...\n",
                "example/management/commands/one.py": "class Command: ...\n",
                "example/management/commands/two.py": "class Command: ...\n",
                "example/tests/test_one.py": "class Thing: ...\n",
            }.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(source)

            self.assertEqual(collisions(root, ("example",)), {"Thing": ["example/a.py", "example/b.py"]})
