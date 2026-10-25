"""No module of HQ imports itself back while it is still being executed.

An import that runs at module level and leads back to its own module hands
that module out half-built, and whether that fails depends on which module
was imported first. Imports that wait (a `lazy` import, one inside a function,
one under `TYPE_CHECKING`) cannot close such a loop, so they are left out.
"""

import ast
from collections.abc import Iterator
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from django.test import SimpleTestCase, tag

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGES = ("hq", "hq_sdk")


def _modules() -> dict[str, Path]:
    found = {}
    for package in PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            parts = path.relative_to(REPO_ROOT).with_suffix("").parts
            found[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    return found


def _waits(node: ast.If) -> bool:
    return "TYPE_CHECKING" in ast.unparse(node.test)


def _executed_imports(body: list[ast.stmt]) -> Iterator[ast.Import | ast.ImportFrom]:
    """The imports a module runs as it executes: its own statements and those
    of the blocks and class bodies it runs on the way, never a function's."""

    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            if not node.is_lazy:
                yield node
            continue
        if isinstance(node, ast.If) and _waits(node):
            yield from _executed_imports(node.orelse)
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            yield from _executed_imports(getattr(node, field, []))


def _named(module: str, path: Path, node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
        anchor = package[: len(package) - node.level + 1]
        base = ".".join([*anchor, base] if base else anchor)
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def import_graph() -> dict[str, set[str]]:
    """Each module, and the modules of this repository it imports as it runs.

    A module's own packages are already being imported when it runs, so an
    edge to one of them closes nothing."""

    modules = _modules()
    graph: dict[str, set[str]] = {}
    for module, path in modules.items():
        edges = graph.setdefault(module, set())
        for node in _executed_imports(ast.parse(path.read_text(encoding="utf-8")).body):
            for name in _named(module, path, node):
                parts = name.split(".")
                for depth in range(1, len(parts) + 1):
                    target = ".".join(parts[:depth])
                    if target in modules and target != module and not module.startswith(f"{target}."):
                        edges.add(target)
    return graph


def cycle_in(graph: dict[str, set[str]]) -> list[str]:
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        return list(error.args[1])
    return []


@tag("source", "tree")
class ImportCycleTests(SimpleTestCase):
    def test_no_module_imports_itself_back_while_it_runs(self):
        cycle = cycle_in(import_graph())
        self.assertFalse(cycle, " -> ".join(cycle))

    def test_a_lazy_import_closes_no_loop_and_an_eager_one_does(self):
        eager = {"example.a": {"example.b"}, "example.b": {"example.a"}}
        self.assertTrue(cycle_in(eager))

        tree = ast.parse("lazy from example.b import g\nfrom example.c import h\n")
        self.assertEqual([node.module for node in _executed_imports(tree.body)], ["example.c"])

    def test_an_import_that_waits_is_not_run_with_the_module(self):
        source = (
            "if TYPE_CHECKING:\n    import example.typed\n"
            "def f():\n    import example.called\n"
            "try:\n    import example.tried\nexcept ImportError:\n    import example.fallback\n"
            "class C:\n    import example.in_class\n"
        )
        imported = [node.names[0].name for node in _executed_imports(ast.parse(source).body)]
        self.assertEqual(imported, ["example.tried", "example.fallback", "example.in_class"])
