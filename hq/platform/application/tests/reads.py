"""The files of this checkout a ``tree`` test reads, declared on its class
and held to the declaration while it runs.

``mise run fast`` runs a declared tree test only on a change its globs
match, and an undeclared one on every change. As in Bazel, where a test's
declared inputs are all its sandbox holds, a read outside the declaration
fails the test: every open of a file under the checkout, every name a
listing yields there (``rglob``, ``os.walk``, ``os.scandir``, ``glob``),
every path it stats (``exists``, ``is_file``), and every program it starts. So a declaration that misses a read fails in
CI's whole run, never by a skipped test going quiet.

A file whose content the test reads is in ``*opened``. One it only lists by
name, or asks whether it is there, is in ``lists``, which a change selects the test by when it adds or
deletes such a file, since an edit leaves every listing as it was.

A program is allowed only where what it reads is known: the walk ``apart``
runs, held to the same declaration through ``ENVIRONMENT``; ``git
ls-files``, a listing of every path, so ``lists`` holds ``**``; ``mise
tasks ls`` or ``info``, which opens mise's config, the task files and each file
mise.toml names for it; and one started outside the checkout, which reads
the fixture it was given and opens each path of the checkout it is handed
(a script). Any other program started in the checkout fails the test. A
path git ignores is no change fast can see, so it is never checked.
"""

import builtins
import functools
import glob
import io
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from threading import local
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
# A copy of the host with no checkout (the extension check's, an image's) has no change for fast to select
# a test by, nor git to say what it ignores, so a declaration there holds nothing and is not watched.
CHECKOUT = (ROOT / ".git").exists()
# The declaration an ``apart`` walk is held to, handed to its process.
ENVIRONMENT = "HQ_TREE_READS"
APART = "hq.platform.application.tests.apart"
# How many reads outside the declaration a failure names.
SHOWN = 200


@dataclass(frozen=True)
class Reads:
    opened: tuple[str, ...]
    listed: tuple[str, ...] = ()

    def opens(self, path: PurePosixPath) -> bool:
        return any(path.full_match(pattern) for pattern in self.opened)

    def lists(self, path: PurePosixPath) -> bool:
        return self.opens(path) or any(path.full_match(pattern) for pattern in self.listed)

    def lists_under(self, path: PurePosixPath) -> bool:
        """Whether every path an add could put at ``path`` or under it is one this lists: what a test that
        found nothing there would see come."""

        return any(_under(PurePosixPath(pattern).parts, path.parts) for pattern in (*self.opened, *self.listed))

    def lists_everything(self) -> bool:
        return "**" in (*self.opened, *self.listed)

    def selects(self, changed: Iterable[str], listed: Iterable[str]) -> bool:
        """Whether a change reaches what this declares: ``changed`` (each path edited, added or deleted)
        names a file it opens, or ``listed`` (each added or deleted) one it lists."""

        return any(self.opens(PurePosixPath(path)) for path in changed) or any(
            self.lists(PurePosixPath(path)) for path in listed
        )


def _under(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Whether ``pattern`` matches ``parts`` and either ends there or goes on as ``**``, matching all below."""

    if not parts:
        return pattern in ((), ("**",))
    if not pattern:
        return False
    if pattern[0] == "**":
        return _under(pattern[1:], parts) or _under(pattern, parts[1:])
    return fnmatchcase(parts[0], pattern[0]) and _under(pattern[1:], parts[1:])


def reads(*opened: str, lists: tuple[str, ...] = ()) -> Callable[[type], type]:
    """Declare the files of this checkout a tree test class reads (``Reads``), and hold its class fixtures,
    setup, tests and teardown to them. Each glob is a literal, since fast reads it from the source."""

    declared = Reads(tuple(opened), tuple(lists))

    def decorate(cls: type) -> type:
        cls.tree_reads = declared  # type: ignore[attr-defined]
        for name in ("setUpClass", "tearDownClass"):
            setattr(cls, name, classmethod(_held(declared, getattr(cls, name).__func__)))
        for name in ("_callSetUp", "_callTestMethod", "_callTearDown"):
            setattr(cls, name, _held(declared, getattr(cls, name)))
        return cls

    return decorate


def _held(declared: Reads, step: Callable[..., Any]) -> Callable[..., Any]:
    if not CHECKOUT:
        return step

    @functools.wraps(step)
    def held(*args: Any, **kwargs: Any) -> Any:
        with watching(declared):
            return step(*args, **kwargs)

    return held


class Watch:
    """The reads one watched step made outside its declaration."""

    def __init__(self, declared: Reads):
        self.declared = declared
        self.misses: set[str] = set()

    def listed(self, path: Any) -> None:
        if (inside := _inside(path)) and not self.declared.lists(inside):
            self.misses.add(f"lists {inside}")

    def missed(self, path: Any) -> None:
        if (inside := _inside(path)) and not self.declared.lists_under(inside):
            self.misses.add(f"looks for {inside}, which an add under it puts there")

    def opened(self, path: Any) -> None:
        if (inside := _inside(path)) and not self.declared.opens(inside):
            self.misses.add(f"opens {inside}")

    def started(self, argv: list[str], cwd: Any) -> None:
        program, words = PurePosixPath(argv[0]).name, argv[1:]
        if words[:2] == ["-m", APART]:
            return
        if program == "git" and "ls-files" in words:
            if not self.declared.lists_everything():
                self.misses.add("runs a listing of every tracked path: declare lists=('**',)")
            return
        if words[:1] in (["-C"], ["--cd"]) and len(words) > 1:
            cwd, words = words[1], words[2:]
        here = cwd is None or _inside(cwd, ignored=False) is not None
        if here and program == "mise" and words[:1] == ["tasks"] and words[1:2] in (["ls"], ["info"]):
            for path in _mise_config():
                self.opened(ROOT / path)
        elif here:
            self.misses.add(f"runs {' '.join(argv[:3])} here, which may read anything")
        else:
            # Run elsewhere, it reads what it is handed of the checkout (a script) as an open; a relative word
            # is a path from its own directory.
            for word in words:
                if PurePosixPath(word).is_absolute():
                    self.opened(word)


_watches: list[Watch] = []
_state = local()


@contextmanager
def watching(declared: Reads) -> Iterator[Watch]:
    """Hold what runs inside to ``declared``, and fail on leaving it with each read that was outside."""

    watch = Watch(declared)
    _load_translations()
    _install()
    _forget_templates()
    _watches.append(watch)
    try:
        yield watch
    finally:
        _watches.remove(watch)
    if watch.misses:
        shown = sorted(watch.misses)
        more = f"\n  ...and {len(shown) - SHOWN} more" if len(shown) > SHOWN else ""
        raise AssertionError(
            "This test reads outside its @reads, so fast would skip it on a change that reaches it. Add a glob"
            " for each, or drop @reads so it runs on every change:\n  " + "\n  ".join(shown[:SHOWN]) + more
        )


def _load_translations() -> None:
    """Load Django's translation catalogs before watching: the process loads them once, probing every app's
    locale directory, on whichever test first renders, so that probe belongs to the process, not the test."""

    from django.apps import apps
    from django.conf import settings
    from django.utils.translation import get_language, trans_real

    # A walk run in a process of its own (apart.py) has no apps loaded, and so no catalog to load.
    if not apps.ready:
        return
    language = get_language() or settings.LANGUAGE_CODE
    trans_real.translation(language)
    trans_real.check_for_language(language)


def _forget_templates() -> None:
    """Empty Django's cached template loaders, so a template an earlier test of the worker loaded is opened
    again where this one reads it, and held to its declaration."""

    if "django.template" not in sys.modules:
        return
    from django.conf import settings
    from django.template import engines

    if not settings.configured:
        return
    for engine in engines.all():
        for loader in getattr(getattr(engine, "engine", None), "template_loaders", ()):
            if hasattr(loader, "reset"):
                loader.reset()


def handing() -> dict[str, str]:
    """The environment for an ``apart`` walk: this one, with the declaration of the watch it runs under, if
    any. Never os.environ itself, which a test that patches it restores as it found it, the declaration
    included, after the watch has ended."""

    environment = {name: value for name, value in os.environ.items() if name != ENVIRONMENT}
    if _watches:
        declared = _watches[-1].declared
        environment[ENVIRONMENT] = json.dumps([declared.opened, declared.listed])
    return environment


def handed() -> Reads | None:
    """The declaration a watched test handed this process, if any."""

    found = os.environ.get(ENVIRONMENT)
    if not found:
        return None
    opened, listed = json.loads(found)
    return Reads(tuple(opened), tuple(listed))


def _active() -> Watch | None:
    if not _watches or getattr(_state, "depth", 0) or _importing():
        return None
    return _watches[-1]


def _importing() -> bool:
    """Whether a module's body is running. Imports are lazy, so a body runs in whichever test first touches
    one of its names, and what it reads belongs to the process; fast selects a test by its imports."""

    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == "exec_module" and frame.f_code.co_filename.startswith("<frozen importlib"):
            return True
        frame = frame.f_back
    return False


@contextmanager
def _unwatched() -> Iterator[None]:
    """What a watched call does inside is its own: rglob's scandir, a template loader's open of what it found."""

    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1


def _inside(path: Any, *, ignored: bool = True) -> PurePosixPath | None:
    """``path`` from the checkout's root, when it lies in the checkout and (with ``ignored``) git does not
    ignore it."""

    if path is None or isinstance(path, int):
        return None
    try:
        # Normalized as written, never resolved: a link inside the checkout is read where it lies.
        text = posixpath.normpath(Path(os.fsdecode(path)).absolute().as_posix())
    except TypeError:
        return None
    root = str(ROOT)
    if text != root and not text.startswith(root + os.sep):
        return None
    relative = PurePosixPath(text[len(root) + 1 :])
    # git keeps itself in .git, which it never lists as ignored, and Python's caches appear as the run goes,
    # after the ignored paths were read.
    if ignored and (not relative.parts or relative.parts[0] == ".git" or "__pycache__" in relative.parts):
        return None
    if ignored and _ignored(relative):
        return None
    return relative


def _ignored(path: PurePosixPath) -> bool:
    found = _ignores()
    return any("/".join(path.parts[:n]) in found for n in range(1, len(path.parts) + 1))


@functools.cache
def _ignores() -> frozenset[str]:
    """What git ignores in the checkout, each ignored directory once."""

    with _unwatched():
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    return frozenset(entry.rstrip("/") for entry in os.fsdecode(listed).split("\0") if entry)


# A file mise.toml has mise read as it loads: an env file, or one a template reads.
MISE_NAMES = re.compile(r"""^_\.file\s*=\s*["']([^"']+)["']|read_file\(path=["']([^"']+)["']\)""", re.MULTILINE)


@functools.cache
def _mise_config() -> tuple[str, ...]:
    """What mise reads to list this checkout's tasks: its config and lock, every task file, and each file
    mise.toml names for it to read."""

    with _unwatched():
        config = (ROOT / "mise.toml").read_text(encoding="utf-8")
        tasks = [path.relative_to(ROOT).as_posix() for path in (ROOT / "mise-tasks").rglob("*") if path.is_file()]
        configs = [path.name for path in ROOT.glob("mise*.toml")]
        named = [first or second for first, second in MISE_NAMES.findall(config)]
        # A file it names may be one git ignores that is not there (an operator's own), which no change shows.
        ignored = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="\n".join(named),
            capture_output=True,
            text=True,
            # It exits 1 when it ignores none of them.
            check=False,
        ).stdout.split()
    return tuple(sorted({*configs, "mise.lock", *tasks, *named} - set(ignored)))


class _Listing:
    """A listing's iterator, each name it yields told to the watch; a context manager when the listing is."""

    def __init__(self, listing: Any, watch: Watch, names: Callable[[Any], Iterable[Any]]):
        self.listing, self.watch, self.names = listing, watch, names
        self.iterator = iter(listing)

    def __iter__(self) -> _Listing:
        return self

    def __next__(self) -> Any:
        with _unwatched():
            item = next(self.iterator)
        for name in self.names(item):
            self.watch.listed(name)
        return item

    def __enter__(self) -> _Listing:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if hasattr(self.listing, "close"):
            self.listing.close()


def _walked(item: tuple[Any, list[Any], list[Any]]) -> list[Any]:
    top, directories, files = item
    return [os.path.join(top, name) for name in (*directories, *files)]


def _beside(directory: Any, name: Any) -> Any:
    """A name a listing gave, from the directory it listed; a listing of an open directory names none."""

    if isinstance(directory, int):
        return None
    return name if directory is None else os.path.join(directory, name)


Names = Callable[[tuple[Any, ...], dict[str, Any], Any], Iterable[Any]]
# Each listing patched, with the names an item it yields stands for, from the
# call's arguments. One that answers a list (os.listdir, glob.glob) is checked
# as it returns.
LISTINGS: tuple[tuple[Any, str, Names], ...] = (
    (Path, "rglob", lambda args, kwargs, item: [item]),
    (Path, "glob", lambda args, kwargs, item: [item]),
    (Path, "iterdir", lambda args, kwargs, item: [item]),
    (Path, "walk", lambda args, kwargs, item: _walked(item)),
    (os, "walk", lambda args, kwargs, item: _walked(item)),
    (os, "scandir", lambda args, kwargs, item: [_beside(args[0] if args else kwargs.get("path"), item.name)]),
    (os, "listdir", lambda args, kwargs, item: [_beside(args[0] if args else kwargs.get("path", "."), item)]),
    (glob, "glob", lambda args, kwargs, item: [_beside(kwargs.get("root_dir"), item)]),
    (glob, "iglob", lambda args, kwargs, item: [_beside(kwargs.get("root_dir"), item)]),
)
EAGER = {(os, "listdir"), (glob, "glob")}


def _listing(owner: Any, name: str, names: Names) -> Callable[..., Any]:
    original = getattr(owner, name)

    @functools.wraps(original)
    def watched(*args: Any, **kwargs: Any) -> Any:
        watch = _active()
        if watch is None:
            return original(*args, **kwargs)
        with _unwatched():
            found = original(*args, **kwargs)
        if (owner, name) in EAGER:
            for item in found:
                for each in names(args, kwargs, item):
                    watch.listed(each)
            return found
        return _Listing(found, watch, lambda item: names(args, kwargs, item))

    return watched


def _opener(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def watched(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        watch = _active()
        if watch is not None and not set(mode) & set("wax+"):
            watch.opened(file)
        with _unwatched():
            return original(file, mode, *args, **kwargs)

    return watched


def _stat(original: Callable[..., Any]) -> Callable[..., Any]:
    """A stat (``exists``, ``is_file``) tells whether a path is there, which only an add or delete moves, so
    it counts as a listing of that path. Not of a directory that is there: git holds files, so it goes only
    with the files under it, which the read that found it names. One that is not there comes with any file
    added under it, so each of those must be declared."""

    @functools.wraps(original)
    def watched(path: Any, *args: Any, **kwargs: Any) -> Any:
        watch = _active()
        if watch is None or kwargs.get("dir_fd") is not None:
            return original(path, *args, **kwargs)
        try:
            with _unwatched():
                found = original(path, *args, **kwargs)
        except OSError:
            watch.missed(path)
            raise
        if not stat.S_ISDIR(found.st_mode):
            watch.listed(path)
        return found

    return watched


def _starter(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def watched(self: Any, args: Any, *rest: Any, **kwargs: Any) -> Any:
        watch = _active()
        if watch is not None:
            argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
            watch.started([os.fsdecode(word) for word in argv], kwargs.get("cwd"))
        with _unwatched():
            return original(self, args, *rest, **kwargs)

    return watched


@functools.cache
def _install() -> None:
    """Patch each listing, open and program start once per process; outside a watch each does as it did."""

    for owner, name, names in LISTINGS:
        setattr(owner, name, _listing(owner, name, names))
    # Not lstat, which resolve() calls on each part of a path whether or not anything reads it.
    os.stat = _stat(os.stat)
    opener = _opener(builtins.open)
    builtins.open = opener
    io.open = opener  # type: ignore[assignment]
    subprocess.Popen.__init__ = _starter(subprocess.Popen.__init__)  # type: ignore[method-assign]
