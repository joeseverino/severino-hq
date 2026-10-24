"""What HQ's dev tools read the same way: a task's parsed flags, the pages a
tool is asked for, and a throwaway checkout of a ref.

mise parses a task's arguments by its usage and hands the script each one as
``usage_<name>`` (lower case, ``-`` as ``_``): an unset flag as nothing, a
switch as ``true`` (``false`` when negated), and a repeated flag or a variadic
argument shell-quoted and joined. :class:`Usage` is the one reader of them, so
no tool declares or parses a flag of its own.

A page is named as dev:look and perf both take it: ``estate/<path>``, a
plain path (``/infrastructure/containers/``), a route name
(``control_plane:containers``), ``estate/all`` or ``all``. Django's
``resolve`` and ``reverse`` say whether it is one; ``difflib`` names the
nearest routes to one that is not.

The worktree is ``git worktree add --detach`` in a ``tempfile`` folder, removed
however the block ends; the overlay of this tree's test files is the one piece
of our own, so a ref's code runs under the tests and lenses this tree has.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

lazy from django.urls import NoReverseMatch, Resolver404, resolve, reverse

lazy from hq.platform.application.routes import routed

ROOT = Path(__file__).resolve().parents[4]
# How mise names a task's argument in the script's environment.
PREFIX = "usage_"
ESTATE = "estate"
# How many routes a refused page is told are nearest.
_NEAREST = 3


# ------------------------------------------------------------------- flags


@dataclass(frozen=True, slots=True)
class Usage:
    """A task's arguments as mise parsed them, by name without the prefix."""

    values: Mapping[str, str]

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Usage:
        environ = os.environ if environ is None else environ
        return cls({key.removeprefix(PREFIX): value for key, value in environ.items() if key.startswith(PREFIX)})

    def given(self, name: str) -> bool:
        """Whether mise set it at all: an unset flag is absent, unlike one given empty."""

        return name in self.values

    def text(self, name: str, default: str = "") -> str:
        return self.values.get(name, default)

    def switch(self, name: str) -> bool:
        return self.values.get(name) == "true"

    def repeated(self, name: str) -> tuple[str, ...]:
        """A flag given more than once, or a variadic argument: mise joins its values shell-quoted."""

        return tuple(shlex.split(self.values.get(name, "")))


def usage() -> Usage:
    """This process's task arguments, read now, so a test's patched environment is the one read."""

    return Usage.from_environment()


# ------------------------------------------------------------------- pages


class PageRefused(Exception):
    """A page asked for that is none, with the nearest routes it might have meant."""


def is_estate(name: str) -> bool:
    return name == ESTATE or name.startswith(f"{ESTATE}/")


def estate_name(address: str) -> str:
    """An estate page's name, as a shot and a report call it."""

    return "/".join(filter(None, (ESTATE, address.strip("/"))))


def estate_path(name: str) -> str:
    """The address an estate page is asked by: its path ends in a slash, and its query, if any, follows untouched."""

    path, _, query = name.removeprefix(ESTATE).partition("?")
    rest = path.strip("/")
    return (f"/{rest}/" if rest else "/") + (f"?{query}" if query else "")


def _routes() -> dict[str, str]:
    """Every route that takes no argument, by name, at its path."""

    found: dict[str, str] = {}
    for route in routed():
        if route.name:
            with suppress(NoReverseMatch):
                found[route.name] = reverse(route.name)
    return found


def _last(name: str) -> str:
    """The last word of a route's name or path: where a page is told from its neighbors."""

    return re.split(r"[/:]", name.strip("/"))[-1]


def nearest(asked: str, named: Sequence[str] = ()) -> list[str]:
    """The routes, and pages known by name, closest to ``asked``: first those whose
    last word is closest to its last word, then those whose whole name or path is."""

    routes = _routes()
    by_key: dict[str, list[str]] = {}
    for name in named:
        by_key.setdefault(name, []).append(name)
    for name, path in routes.items():
        for key in (name, path):
            by_key.setdefault(key, []).append(name)
    by_last: dict[str, list[str]] = {}
    for key, names in by_key.items():
        by_last.setdefault(_last(key), []).extend(names)
    matched = [
        *(name for match in difflib.get_close_matches(_last(asked), by_last, cutoff=0.6) for name in by_last[match]),
        *(name for match in difflib.get_close_matches(asked, by_key, cutoff=0.5) for name in by_key[match]),
    ]
    said = [f"{name} ({routes[name]})" if name in routes else name for name in dict.fromkeys(matched)]
    return said[:_NEAREST]


def _address(name: str) -> str | None:
    """The estate address ``name`` names, or None for a name that is no route."""

    if not (is_estate(name) or name.startswith("/")):
        try:
            return reverse(name)
        except NoReverseMatch:
            return None
    address = estate_path(name if is_estate(name) else f"{ESTATE}{name}")
    try:
        resolve(urlsplit(address).path)
    except Resolver404:
        return None
    return address


def pages(asked: Sequence[str], every: Sequence[str], named: Sequence[str] = ()) -> list[str]:
    """The pages ``asked`` names, in order and once each: an estate page as its
    address, a page known by name (``named``, dev:look's gate pages) as its
    name. ``every`` is the addresses the estate's navigation lists, which
    ``estate/all`` and nothing asked stand for; ``all`` is those and ``named``.
    Every name that is no page is refused together, each with its nearest."""

    found, refused = [], []
    for name in asked or ["all"]:
        if name == "all":
            found += [*named, *every]
        elif name == f"{ESTATE}/all":
            found += every
        elif name in named:
            found.append(name)
        elif (address := _address(name)) is not None:
            found.append(address)
        else:
            close = nearest(urlsplit(estate_path(name)).path if is_estate(name) else name, named)
            refused.append(f"No page {name}" + (f"; the nearest: {', '.join(close)}" if close else ""))
    if refused:
        kinds = "estate/<path>, a path, a route name" + (", a page by name" if named else "")
        raise PageRefused("\n".join([*refused, f"A page is {kinds}, estate/all or all."]))
    return list(dict.fromkeys(found))


# ------------------------------------------------------------------- a ref's worktree


class RefError(Exception):
    """The ref names no commit, or its worktree cannot be made."""


def git(*args: str, root: Path = ROOT) -> list[str]:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if done.returncode:
        raise RefError(done.stderr.strip() or f"git {args[0]} exited {done.returncode}")
    return [line for line in done.stdout.splitlines() if line]


def commit_of(ref: str, root: Path = ROOT) -> str:
    """The full commit ``ref`` names."""

    try:
        return git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", root=root)[0]
    except RefError:
        raise RefError(f"{ref!r} names no commit") from None


def is_test_file(path: str) -> bool:
    """A file the suite runs or builds its cases from, rather than the code under test."""

    return "tests" in Path(path).parts[:-1]


def changed_tests(commit: str, root: Path = ROOT) -> tuple[list[str], list[str]]:
    """(present, deleted) test files where this tree, uncommitted work included, differs from the commit."""

    present = set(git("diff", "--name-only", "--no-renames", "--diff-filter=ACMT", commit, root=root))
    present |= set(git("ls-files", "--others", "--exclude-standard", root=root))
    deleted = git("diff", "--name-only", "--no-renames", "--diff-filter=D", commit, root=root)
    return sorted(filter(is_test_file, present)), sorted(filter(is_test_file, deleted))


def overlay(tree: Path, present: Sequence[str], deleted: Sequence[str], root: Path = ROOT) -> None:
    """This tree's copy of each path in ``present`` laid over ``tree``, and each in ``deleted`` taken out."""

    for path in present:
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, tree / path)
    for path in deleted:
        (tree / path).unlink(missing_ok=True)


def _stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def worktree(commit: str, root: Path = ROOT) -> Iterator[Path]:
    """A detached worktree of ``commit``, outside this checkout so mise reads only its config,
    beside a folder of its own for what a run there leaves; both gone however the block ends,
    a termination raised as an exit so it unwinds through here too."""

    previous = signal.signal(signal.SIGTERM, _stop)
    try:
        with tempfile.TemporaryDirectory(prefix="hq-worktree-") as folder:
            tree = Path(folder) / "checkout"
            try:
                git("worktree", "add", "--detach", "--quiet", str(tree), commit, root=root)
                yield tree
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=root, capture_output=True)
                subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)
    finally:
        signal.signal(signal.SIGTERM, previous)
