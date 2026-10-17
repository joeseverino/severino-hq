"""Parsed source files, cached per test process.

Keyed on modification time, so a file edited mid-run is parsed again.
"""

import ast
from functools import cache
from pathlib import Path


def parsed(path: Path) -> ast.Module:
    """The module ``path`` holds, as an AST its callers read and never change."""

    return _parsed(Path(path).resolve(), Path(path).stat().st_mtime_ns)


@cache
def _parsed(path: Path, _modified: int) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
