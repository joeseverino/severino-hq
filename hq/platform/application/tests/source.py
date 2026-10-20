"""This checkout's source files, and their parsed form cached per test process.

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


# Installed or generated trees inside a checkout. With hidden directories
# (.git, a virtualenv under any name) they are not this project's source.
NOT_SOURCE = {
    "venv",
    "site-packages",
    "node_modules",
    "staticfiles",
    "build",
    "data",
    "var",
    "media",
    "exports",
    "backups",
}


def source_files(root: Path, pattern: str) -> list[Path]:
    """This checkout's own files matching ``pattern``.

    Read from the directory tree, not from git: the suite also runs inside the
    image, where there is no git to ask what is tracked.
    """

    return sorted(
        path
        for path in root.rglob(pattern)
        if not any(part.startswith(".") or part in NOT_SOURCE for part in path.relative_to(root).parts[:-1])
    )
