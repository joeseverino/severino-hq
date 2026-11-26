"""`mise run lint:lazy-imports [files]`: every name a ``lazy from`` import takes is one its module defines.

A lazy import resolves its name on first use, so a name its module lacks
loads cleanly and fails only in the code that reaches it. mypy reports the
missing name as ``attr-defined``, which the reach pass leaves off for the
modules the seams pass does not hold, and Ruff reads one file at a time. So
this reads each module's top-level names through ``ast`` and imports nothing.
Handed files, it checks the lazy imports in them and every lazy import of a
module among them; handed none, every one in the checkout.
"""

import ast
import subprocess
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path

# A name bound by a statement that runs at module level; a def or class body is its own scope.
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def module_file(dotted: str) -> Path | None:
    """The checkout's file for a module, or None for one outside it (a dependency, the standard library)."""
    base = Path(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def bound(node: ast.AST) -> Iterator[str]:
    for child in ast.iter_child_nodes(node):
        match child:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                yield child.name
            case ast.Import() | ast.ImportFrom():
                yield from ((alias.asname or alias.name).partition(".")[0] for alias in child.names)
            case ast.Name(ctx=ast.Store()):
                yield child.id
            case ast.TypeAlias(name=ast.Name(id=name)) | ast.ExceptHandler(name=str(name)):
                yield name
            case ast.MatchAs(name=str(name)) | ast.MatchStar(name=str(name)) | ast.MatchMapping(rest=str(name)):
                yield name
        if not isinstance(child, SCOPES):
            yield from bound(child)


@cache
def names(path: Path) -> frozenset[str] | None:
    """What a module defines at its top level, or None when any name may resolve there."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    defined = frozenset(bound(tree))
    starred = any(isinstance(node, ast.ImportFrom) and node.names[0].name == "*" for node in ast.walk(tree))
    return None if starred or "__getattr__" in defined else defined


def target(path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = path.parent.parts[: len(path.parent.parts) - node.level + 1]
    return ".".join([*package, *([node.module] if node.module else [])])


def lazy_imports(path: Path) -> Iterator[tuple[ast.ImportFrom, str]]:
    source = path.read_text(encoding="utf-8")
    if "lazy " not in source:
        return
    for node in ast.parse(source, str(path)).body:
        if isinstance(node, ast.ImportFrom) and node.is_lazy:
            yield node, target(path, node)


def missing(path: Path, node: ast.ImportFrom, module: str) -> Iterator[str]:
    home = module_file(module)
    if home is None:
        return
    defined = names(home)
    for alias in node.names:
        submodule = home.name == "__init__.py" and module_file(f"{module}.{alias.name}")
        if defined is not None and alias.name not in defined and not submodule:
            yield f"{path}:{node.lineno}: lazy from {module} import {alias.name}: {home} defines no {alias.name}"


def main(named: list[str]) -> int:
    listed = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.py"]
    every = [
        Path(name)
        for name in subprocess.run(listed, capture_output=True, text=True, check=True).stdout.split("\0")
        if name
    ]
    changed = {Path(name) for name in named if name.endswith(".py")}
    failures = [
        failure
        for path in every
        if path.is_file()
        for node, module in lazy_imports(path)
        if not named or path in changed or module_file(module) in changed
        for failure in missing(path, node, module)
    ]
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
