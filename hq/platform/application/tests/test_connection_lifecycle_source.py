"""A connection's lifecycle is said in one module, and every view reads it there."""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

from ..connection_lifecycle import UNREPORTED_FACT, UNREPORTED_NODE, Lifecycle
from .source import parsed, source_files

HOME = Path(__file__).resolve().parents[1] / "connection_lifecycle.py"
SOURCE = Path(__file__).resolve().parents[3]


def _strings(tree: ast.AST):
    """Every string a module spells."""

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, node.value


def _collections(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            yield node, node.keys
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            yield node, node.elts


@tag("source", "tree")
class LifecycleDeclaredOnceTests(SimpleTestCase):
    def modules(self):
        found = [
            path
            for path in source_files(SOURCE, "*.py")
            if "tests" not in path.parts and "migrations" not in path.parts and path != HOME
        ]
        self.assertTrue(found)
        return found

    def test_no_other_module_lists_lifecycle_states(self):
        """A label table or a set of states spelled as strings restates the vocabulary."""

        values = {str(state) for state in Lifecycle}
        for path in self.modules():
            for node, items in _collections(parsed(path)):
                named = {item.value for item in items if isinstance(item, ast.Constant)} & values
                with self.subTest(path=str(path.relative_to(SOURCE)), line=node.lineno):
                    self.assertLess(len(named), 2, sorted(named))

    def test_the_unreported_marker_is_spelled_only_in_its_module(self):
        label = Lifecycle.UNREPORTED.label.lower()
        for path in self.modules():
            for node, text in _strings(parsed(path)):
                with self.subTest(path=str(path.relative_to(SOURCE)), line=node.lineno):
                    self.assertNotEqual(text.lower(), label)
                    self.assertNotIn(UNREPORTED_FACT, text)
                    self.assertNotIn(UNREPORTED_NODE, text)

    def test_the_markers_keep_the_values_already_stored(self):
        self.assertEqual(UNREPORTED_FACT, "connection-unreported")
        self.assertEqual(UNREPORTED_NODE, "connection:unreported:")
