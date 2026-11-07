"""Every ``operation=`` a call in this checkout names, read in a process of its own through ``apart``."""

import ast
from pathlib import Path

from hq.platform.application.tests.source import source_files


def _named(value: ast.expr) -> tuple[str, str] | None:
    """What an ``operation=`` value names: a literal, a module-level name, or an
    f-string as written; None for anything else (a callable, an attribute read
    at run time)."""

    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return "literal", value.value
    if isinstance(value, ast.Name):
        return "name", value.id
    if isinstance(value, ast.JoinedStr):
        return "fstring", ast.unparse(value)
    return None


def bindings(root: str) -> list[list[str]]:
    """``[path, line, kind, text]`` for each ``operation=`` keyword under ``root``,
    tests left out: what they bind is an example, never a row HQ writes."""

    base = Path(root)
    found = []
    for path in source_files(base, "*.py"):
        relative = path.relative_to(base)
        if "tests" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        keywords = (keyword for node in ast.walk(tree) if isinstance(node, ast.Call) for keyword in node.keywords)
        found.extend(
            [str(relative), str(keyword.value.lineno), *named]
            for keyword in keywords
            if keyword.arg == "operation" and (named := _named(keyword.value)) is not None
        )
    return found
