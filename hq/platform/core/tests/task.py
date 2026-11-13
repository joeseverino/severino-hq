"""What HQ's dev tools read the same way: a task's parsed flags, the pages a
tool is asked for, and a throwaway checkout of a ref.

mise parses a task's arguments by its usage and hands the script each one as
``usage_<name>`` (lower case, ``-`` as ``_``): an unset flag as nothing, a
switch as ``true`` (``false`` when negated), and a repeated flag or a variadic
argument shell-quoted and joined. :class:`Usage` is the one reader of them, so
no tool declares or parses a flag of its own.

A page is named as dev:look and perf both take it: ``estate/<path>``, a
plain path (``/infrastructure/containers/``), a route name
(``control_plane:containers``), ``estate/all`` or ``all``. Where the tool
can draw its estate (:class:`Records`), a record's page is named too: by a
route that takes a record (``control_plane:detail``), its first record the
estate's pages link to, or by a template (``template:<name>``), the first
estate page that draws it. Django's ``resolve`` and ``reverse`` say whether a
name is a page; ``difflib`` names the nearest to one that is not, each as the
name to type.

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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

lazy from django.urls import NoReverseMatch, Resolver404, resolve, reverse

lazy from hq.platform.application.routes import routed

ROOT = Path(__file__).resolve().parents[4]
# How mise names a task's argument in the script's environment.
PREFIX = "usage_"
ESTATE = "estate"
# A page named by a template it draws.
TEMPLATE = "template:"
# How many names a refused page is offered.
_NEAREST = 5
_RECORDS_OFFERED = 5


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
    """A page asked for that is none, with the names it would take instead."""


@dataclass(frozen=True, slots=True)
class Drawn:
    """A page of the estate as its server drew it: every template it drew."""

    templates: frozenset[str] = frozenset()

    def draws(self, template: str) -> bool:
        """Whether it drew ``template``, named whole or by its last parts (``_container_detail.html``)."""

        return any(name == template or name.endswith(f"/{template}") for name in self.templates)


class Records(Protocol):
    """The estate a tool draws, asked only when a name needs a record of it."""

    def linked(self) -> Iterable[str]:
        """Every page the navigation reaches and every record's page those link to, by address, in order,
        read as it is asked for, so a name found early waits on no more of the estate."""

    def at(self, address: str) -> Drawn | None:
        """The page ``address`` lands on, or None where the estate answers it with anything but a page."""


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


def _record_routes() -> set[str]:
    """Every named route that takes a record: those ``_routes`` cannot reverse bare."""

    return {route.name for route in routed() if route.name} - set(_routes())


def _last(name: str) -> str:
    """The last word of a route's name or path: where a page is told from its neighbors."""

    return re.split(r"[/:]", name.strip("/"))[-1]


def _words(name: str) -> str:
    """A name or path as its words alone, so a path and a route name compare by what they say."""

    return re.sub(r"[-_:/]+", " ", name).strip()


def _offered(offers: Sequence[str]) -> str:
    """The names a refused page would take, one a line, each as it is typed and then what it is."""

    return "".join(f"\n  {offer}" for offer in offers)


def nearest(asked: str, named: Sequence[str] = ()) -> list[str]:
    """The pages closest to ``asked``, each as the name that reaches it, then what it is: first
    those whose last word is closest to its last word, those sharing one last word in the order
    their whole name or path is close to it, then those whose whole name or path is. A route is
    offered as its estate page, which no page known by name can shadow."""

    by_key: dict[str, list[str]] = {}
    for name in named:
        by_key.setdefault(name, []).append(f"{name} (a page by name)")
    for name, path in _routes().items():
        for key in (name, path):
            by_key.setdefault(key, []).append(f"{estate_name(path)} (the estate's {name})")
    by_last: dict[str, list[tuple[str, str]]] = {}
    for key, offers in by_key.items():
        by_last.setdefault(_last(key), []).extend((key, offer) for offer in offers)
    words = _words(asked)
    matched = [
        *(
            offer
            for match in difflib.get_close_matches(_last(asked), by_last, cutoff=0.6)
            for _, offer in sorted(
                by_last[match], key=lambda entry: -difflib.SequenceMatcher(None, words, _words(entry[0])).ratio()
            )
        ),
        *(offer for match in difflib.get_close_matches(asked, by_key, cutoff=0.5) for offer in by_key[match]),
    ]
    return list(dict.fromkeys(matched))[:_NEAREST]


def _route(address: str) -> str:
    return resolve(urlsplit(address).path).view_name


def _drawn(records: Records) -> Iterator[tuple[str, Drawn]]:
    """Each page of the estate the server draws, with its address."""

    for address in records.linked():
        if page := records.at(address):
            yield address, page


def _of_route(route: str, records: Records) -> Iterator[str]:
    """The estate's records of ``route`` it answers, each drawn only once its address is reached."""

    return (address for address in records.linked() if _route(address) == route and records.at(address))


def _takes_a_record(address: str) -> bool:
    match = resolve(urlsplit(address).path)
    return bool(match.args or match.kwargs)


def _record(name: str, records: Records | None) -> str:
    """The address of the estate page a record's name reaches: a route that
    takes a record, its first one the estate links to, or ``template:<name>``,
    the first estate page that draws it."""

    if records is None:
        raise PageRefused(f"No page {name}: here a record's page is named by its path, estate/<path>")
    if name.startswith(TEMPLATE):
        template = name.removeprefix(TEMPLATE)
        if address := next((address for address, page in _drawn(records) if page.draws(template)), None):
            return address
        by_last: dict[str, list[str]] = {}
        for known in sorted({known for _, page in _drawn(records) for known in page.templates}):
            by_last.setdefault(known.rsplit("/", 1)[-1], []).append(known)
        close = difflib.get_close_matches(template.rsplit("/", 1)[-1], by_last, n=_NEAREST, cutoff=0.6)
        offers = [f"{TEMPLATE}{known}" for match in close for known in by_last[match]]
        said = f"No estate page draws {template}" + ("; type one of these instead:" if offers else "")
        raise PageRefused(said + _offered(offers[:_NEAREST]))
    if address := next(_of_route(name, records), None):
        return address
    linked = sorted({_route(address) for address in records.linked()} & _record_routes())
    offers = difflib.get_close_matches(name, linked, n=_NEAREST, cutoff=0.5)
    said = f"No record of {name} is linked from the estate's pages" + ("; type one of these instead:" if offers else "")
    raise PageRefused(said + _offered([f"{offer} (its first record)" for offer in offers]))


def _no_record(address: str, records: Records) -> str:
    """A record's address the estate does not answer, refused with that route's records it does."""

    route = _route(address)
    theirs = [estate_name(known) for known in islice(_of_route(route, records), _RECORDS_OFFERED)]
    offers = [f"{route} (its first record)", *theirs] if theirs else []
    said = f"No record at {estate_name(address)} in the estate" + ("; type one of these instead:" if offers else "")
    return said + _offered(offers)


def _address(name: str) -> str | None:
    """The estate address ``name`` names, its query kept as an address keeps
    it, or None for a name that is no route: a route name (``search?q=hq``),
    a path, its leading slash optional once it holds one (``search/?q=hq``),
    or ``estate/<path>``."""

    if not (is_estate(name) or name.startswith("/")):
        route, mark, query = name.partition("?")
        with suppress(NoReverseMatch):
            return reverse(route) + mark + query
        if "/" not in route:
            return None
        name = f"/{name}"
    address = estate_path(name if is_estate(name) else f"{ESTATE}{name}")
    try:
        resolve(urlsplit(address).path)
    except Resolver404:
        return None
    return address


def _one_page(name: str, named: Sequence[str], records: Records | None, record_routes: set[str]) -> str:
    """The address of the one page ``name`` asks for, a record's or the
    estate's, or PageRefused saying why it is none, with the names it would take."""

    if name.startswith(TEMPLATE) or name in record_routes:
        return _record(name, records)
    if (address := _address(name)) is None:
        offers = nearest(urlsplit(estate_path(name)).path if is_estate(name) else name, named)
        raise PageRefused(f"No page {name}" + ("; type one of these instead:" if offers else "") + _offered(offers))
    if records is not None and _takes_a_record(address) and records.at(address) is None:
        raise PageRefused(_no_record(address, records))
    return address


def pages(
    asked: Sequence[str], every: Sequence[str], named: Sequence[str] = (), records: Records | None = None
) -> list[str]:
    """The pages ``asked`` names, in order and once each: an estate page as its
    address, a page known by name (``named``, dev:look's gate pages) as its
    name. ``every`` is the estate's pages the browser gate reads, which
    ``estate/all`` and nothing asked stand for; ``all`` is those and ``named``.
    ``records`` is the estate drawn, which a record's page is found in and
    checked against, and a record's page of ``every`` it does not hold is left
    out of both. Every name that is no page is refused together, each with the
    names it would take."""

    found, refused = [], []
    record_routes = _record_routes()

    def estate() -> list[str]:
        if records is None:
            return list(every)
        return [address for address in every if not _takes_a_record(address) or records.at(address) is not None]

    for name in asked or ["all"]:
        if name == "all":
            found += [*named, *estate()]
        elif name == f"{ESTATE}/all":
            found += estate()
        elif name in named:
            found.append(name)
        else:
            try:
                found.append(_one_page(name, named, records, record_routes))
            except PageRefused as said:
                refused.append(str(said))
    if refused:
        kinds = "estate/<path>, a path, a route name" + (", a page by name" if named else "")
        kinds += ", a route that takes a record, template:<name>" if records is not None else ""
        raise PageRefused("\n".join([*refused, f"A page is {kinds}, estate/all or all."]))
    return list(dict.fromkeys(found))


# ------------------------------------------------------------------- a ref's worktree


class RefError(Exception):
    """The ref names no commit, or its worktree cannot be made."""


def _output(args: Sequence[str], root: Path) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if done.returncode:
        raise RefError(done.stderr.strip() or f"git {args[0]} exited {done.returncode}")
    return done.stdout


def git(*args: str, root: Path = ROOT) -> list[str]:
    """The lines a git command prints. A list of file names is ``names``'."""

    return [line for line in _output(args, root).splitlines() if line]


def names(*args: str, root: Path = ROOT) -> list[str]:
    """The file names a git command lists, asked for NUL-separated (``-z`` after the
    subcommand), since in a list of lines git quotes a name holding a backslash, a
    double quote or a newline."""

    command = next(i for i, arg in enumerate(args) if not arg.startswith("-")) + 1
    listed = _output([*args[:command], "-z", *args[command:]], root)
    return [name for name in listed.split("\0") if name]


def commit_of(ref: str, root: Path = ROOT) -> str:
    """The full commit ``ref`` names."""

    try:
        return git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", root=root)[0]
    except RefError:
        raise RefError(f"{ref!r} names no commit") from None


def is_test_file(path: str) -> bool:
    """A file the suite runs or builds its cases from, rather than the code under test."""

    return "tests" in Path(path).parts[:-1]


def changed_paths(commit: str, root: Path = ROOT) -> tuple[list[str], list[str]]:
    """(present, deleted) paths where this tree, uncommitted work included, differs from the commit.
    A rename is its two halves, so the old path is among the deleted."""

    present = set(names("diff", "--name-only", "--no-renames", "--diff-filter=ACMT", commit, root=root))
    present |= set(names("ls-files", "--others", "--exclude-standard", root=root))
    deleted = names("diff", "--name-only", "--no-renames", "--diff-filter=D", commit, root=root)
    return sorted(present), sorted(deleted)


def changed_tests(
    commit: str, root: Path = ROOT, is_test: Callable[[str], bool] = is_test_file
) -> tuple[list[str], list[str]]:
    """(present, deleted) test files, as ``is_test`` tells them, of ``changed_paths``."""

    present, deleted = changed_paths(commit, root)
    return list(filter(is_test, present)), list(filter(is_test, deleted))


def overlay(tree: Path, present: Sequence[str], deleted: Sequence[str], root: Path = ROOT) -> None:
    """This tree's copy of each path in ``present`` laid over ``tree``, and each in ``deleted`` taken out."""

    for path in present:
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, tree / path)
    for path in deleted:
        (tree / path).unlink(missing_ok=True)


def _stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def trusted_mise(config: Path | str, state: Path | str) -> dict[str, str]:
    """The environment that has mise trust ``config`` and keep its records of that run in ``state``.

    mise records each config it trusts or loads in its state directory, and its reshim walks every
    record there, so a throwaway project's records go with it rather than into the Mac's. The
    caller's own checkout is trusted only in the real state, so no command run on that checkout gets it."""

    return {"MISE_TRUSTED_CONFIG_PATHS": str(config), "MISE_STATE_DIR": str(state)}


# The lockfile of the lint tools a tree pins, axe-core among them, which the survey reads.
LINT_TOOLS_LOCK = Path("tools", "lint", "package-lock.json")


def set_up_lint_tools(tree: Path) -> None:
    """Install the lint tools ``tree``'s own lockfile pins, by its own setup task, so a run
    there reads the axe-core release that ref pins; a ref older than the lockfile has none."""

    if not (tree / LINT_TOOLS_LOCK).is_file():
        return
    env = {**os.environ, **trusted_mise(tree, tree.parent / "mise-state")}
    done = subprocess.run(
        ["mise", "-C", str(tree), "run", "setup:lint-tools"], env=env, capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise RuntimeError(f"setup:lint-tools failed in {tree}:\n{done.stdout}{done.stderr}")


@contextmanager
def worktree(commit: str, root: Path = ROOT, *, lint_tools: bool = False) -> Iterator[Path]:
    """A detached worktree of ``commit``, outside this checkout so mise reads only its config,
    beside a folder of its own for what a run there leaves; both gone however the block ends,
    a termination raised as an exit so it unwinds through here too. With ``lint_tools``,
    for a run that may survey a page, the tree has the lint tools it pins installed."""

    previous = signal.signal(signal.SIGTERM, _stop)
    try:
        with tempfile.TemporaryDirectory(prefix="hq-worktree-") as folder:
            tree = Path(folder) / "checkout"
            try:
                git("worktree", "add", "--detach", "--quiet", str(tree), commit, root=root)
                if lint_tools:
                    set_up_lint_tools(tree)
                yield tree
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(tree)], cwd=root, capture_output=True, check=False
                )
                subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, check=False)
    finally:
        signal.signal(signal.SIGTERM, previous)
