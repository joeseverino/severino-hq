"""This checkout's source files and the templates Django loads."""

import os
from collections.abc import Callable, Sequence
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

lazy from django.template.autoreload import get_template_directories
lazy from django.test import tag

lazy from hq.platform.core.tests.task import changed_paths

ROOT = Path(__file__).resolve().parents[4]
# The ref fast reads its change from, which its test run is handed (tools/gate/fast.py, BASE).
FAST_BASE = "HQ_FAST_BASE"


def walks[T](*globs: str) -> Callable[[T], T]:
    """A test class that reads the files ``globs`` match, as mise reads a task's sources (``**``, and ``!``
    taking paths out): fast selects it when a change touches one (tools/gate/selection.py, ``walking``), and
    there it reads only those (``walked``). Tagged source, since no image ships those files."""

    def declared(test: T) -> T:
        # Any test class, which declares no such attribute for the type checker.
        setattr(test, "walks", globs)  # noqa: B010
        return tag("source")(test)

    return declared


def matches(path: str, globs: Sequence[str]) -> bool:
    pure = PurePosixPath(path)
    return any(pure.full_match(glob) for glob in globs if not glob.startswith("!")) and not any(
        pure.full_match(glob[1:]) for glob in globs if glob.startswith("!")
    )


def walked(globs: Sequence[str]) -> list[str] | None:
    """The files of ``globs`` a walk reads: in fast's run, those its change touched; else, or when the change
    touched none (so the check's own code changed), every one, as None."""

    base = os.environ.get(FAST_BASE)
    if not base:
        return None
    return [path for path in changed_paths(base, ROOT)[0] if matches(path, globs)] or None


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

    found: list[Path] = []
    for top, directories, files in root.walk():
        # Pruned as the walk goes, so it never lists a virtualenv or node_modules it would throw away.
        directories[:] = [name for name in directories if not name.startswith(".") and name not in NOT_SOURCE]
        found += [top / name for name in files if fnmatchcase(name, pattern)]
    return sorted(found)


def template_files() -> list[tuple[str, Path]]:
    """Every template Django loads, the host's and each installed extension's,
    by the name Django loads it by, so a template check holds an extension's
    templates as it holds the host's."""

    found = sorted(
        (path.relative_to(directory).as_posix(), path)
        for directory in get_template_directories()
        for path in directory.rglob("*.html")
    )
    if not found:
        # A template check that reads nothing passes.
        raise AssertionError("No template directory Django names holds a template.")
    return found
