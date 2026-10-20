"""How HQ compares two repositories is decided in one module.

A repository is ``owner/name`` and GitHub ignores its case. A second place
that builds that key from a URL is a second rule, and the copies drift: one
lower-cases and the next does not, so a project cased unlike GitHub's name
quietly stops matching.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]
HOME = "hq/platform/application/projects.py"


def _slash_join(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and node.func.value.value == "/"
    )


def _keys_by_join(node: ast.AST) -> bool:
    """``"/".join(parts).lower()``, a mapping keyed by ``"/".join(parts)``, or a lookup by one.

    Joining parts back into ``owner/name`` for display or a signer identity is
    a name, not a key, and is left alone.
    """

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "lower" and _slash_join(node.func.value):
            return True
        if node.func.attr == "get" and node.args and _slash_join(node.args[0]):
            return True
    return isinstance(node, ast.DictComp) and _slash_join(node.key)


@tag("source")
class OneRepositoryKeyTests(SimpleTestCase):
    def test_only_the_projects_service_builds_a_repository_key(self):
        offenders = []
        for path in source_files(ROOT, "*.py"):
            relative = str(path.relative_to(ROOT))
            if relative == HOME or "/tests/" in relative or "/migrations/" in relative:
                continue
            offenders.extend(f"{relative}:{node.lineno}" for node in ast.walk(parsed(path)) if _keys_by_join(node))
        self.assertEqual(offenders, [])

    def test_the_guard_reads_the_module_that_builds_it(self):
        functions = [node for node in ast.walk(parsed(ROOT / HOME)) if isinstance(node, ast.FunctionDef)]
        home = [node.name for node in functions if any(_keys_by_join(inner) for inner in ast.walk(node))]
        self.assertEqual(home, ["repository_key"])
