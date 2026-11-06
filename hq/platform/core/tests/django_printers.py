"""The nodes the installed Django prints a template value through, read from its source.

It parses each module of Django's template package, so the print gate's test
runs it through ``apart``. The installed Django is read, not this checkout,
since the image runs the suite without ast-grep.
"""

import ast
from pathlib import Path

import django


def _printers(path: Path, names: set[str]) -> set[str]:
    """The top-level classes and functions in ``path`` that call one of ``names``."""

    return {
        statement.name
        for statement in ast.parse(path.read_text()).body
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef))
        and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in names
            for call in ast.walk(statement)
        )
    }


def django_printers() -> list[str]:
    """Each class or function of Django's template code that prints a value, by its dotted name."""

    installed = Path(django.__file__).parent
    found = {
        f"{'.'.join(path.relative_to(installed.parent).with_suffix('').parts)}.{name}"
        for path in (*installed.glob("template/**/*.py"), *installed.glob("templatetags/*.py"))
        for name in _printers(path, {"render_value_in_context"})
    }
    found |= {
        f"django.template.library.{name}"
        for name in _printers(installed / "template" / "library.py", {"conditional_escape"})
    }
    return sorted(found)
