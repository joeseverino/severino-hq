"""The setting names this checkout overrides, read in a process of its own through ``apart``."""

import ast
from pathlib import Path

from hq.platform.application.tests.source import source_files


def _is_override(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "override_settings"
    if isinstance(func, ast.Attribute):
        if func.attr == "override_settings":
            return True
        # TestCase.settings, the context manager form.
        return func.attr == "settings" and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}
    return False


def _named(call: ast.Call) -> list[str]:
    """The setting names a call overrides by keyword, a ``**{...}`` literal's keys included."""

    names = []
    for keyword in call.keywords:
        if keyword.arg is not None:
            names.append(keyword.arg)
        elif isinstance(keyword.value, ast.Dict):
            names.extend(key.value for key in keyword.value.keys if isinstance(key, ast.Constant))
    return names


def overridden(root: str) -> dict[str, list[str]]:
    """Each setting name overridden under ``root``, with every ``path:line`` that overrides it."""

    found: dict[str, list[str]] = {}
    base = Path(root)
    for path in source_files(base, "*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_override(node):
                for name in _named(node):
                    found.setdefault(name, []).append(f"{path.relative_to(base)}:{node.lineno}")
    return found
