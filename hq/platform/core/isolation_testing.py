"""What each test of a run starts from beyond its database: the process caches,
the agent limits and the controller's markers, isolated as the rows are."""

import contextlib
import functools
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import LazyImportType
from typing import TYPE_CHECKING
from unittest import mock

from django.conf import settings

from hq.config.sandbox import MARKERS
lazy from hq.platform.application import agent_limits, automation_adoption, plugins

if TYPE_CHECKING:
    from hq.platform.application.agent_limits import Limits


# What mock's patches left by before this process wrapped it.
_left_by = None


def patches_leave_no_copy() -> None:
    """When a ``mock.patch`` ends, no module still holds its mock under the name it patched or an alias of it.

    Imports are lazy (``hq.config.imports``): ``from .domains import
    all_domains`` binds when the name is first used, or when its module first
    loads, and either can happen while a test patches ``domains.all_domains``.
    The importer then holds the mock, which the patch never put there and so
    never takes back, and every later test in the process reads it. Each such
    copy is given back the patched name's own value.
    """

    global _left_by  # noqa: PLW0603 - the door is wrapped once a process
    if _left_by is None:
        _left_by = mock._patch.__exit__  # noqa: SLF001 - the one door every patch leaves by
        mock._patch.__exit__ = _taking_back_copies(_left_by)  # noqa: SLF001 - as above
        # And the one every patch comes in by, so a module loaded before it
        # has its lazy aliases noted before the patch can bind them.
        mock._patch.__enter__ = _noting_aliases(mock._patch.__enter__)  # noqa: SLF001 - as above


def _noting_aliases(enter):
    @functools.wraps(enter)
    def entering(patcher):
        _read_new_modules()
        return enter(patcher)

    return entering


def _taking_back_copies(leave):
    @functools.wraps(leave)
    def leaving(patcher, *exc_info):
        name = patcher.attribute
        started = patcher.is_started
        target = patcher.target if started else None
        replaced = _held(target, name) if started else None
        try:
            return leave(patcher, *exc_info)
        finally:
            if started:
                _give_back(name, replaced, _held(target, name))

    return leaving


def _held(target: object, name: str) -> object:
    """What ``target`` holds under ``name``, read from its namespace first as
    mock reads an original, so a lazy import put back stays unimported: its
    module may be one this environment lacks."""

    try:
        namespace = vars(target)
    except TypeError:
        return getattr(target, name, None)
    return namespace[name] if name in namespace else getattr(target, name, None)


def _give_back(name: str, replaced: object, original: object) -> None:
    _read_new_modules()
    for module in list(sys.modules.values()):
        # An entry can be a stand-in with no namespace (None blocks an import).
        with contextlib.suppress(TypeError):
            if vars(module).get(name) is replaced:
                vars(module)[name] = original
    for key, alias in tuple(_aliased.get(name, ())):
        with contextlib.suppress(TypeError):
            namespace = vars(sys.modules.get(key))
            if namespace.get(alias) is replaced:
                namespace[alias] = original


# The modules read for aliases, by name; and each name a lazy import binds
# under another (``from x import f as g``), with each module and the name it
# binds there.
_read: set[str] = set()
_last_read: tuple[int, str | None] = (0, None)
_aliased: dict[str, set[tuple[str, str]]] = {}


def _read_new_modules() -> None:
    """Note each name a module loaded since the last patch came or went
    imports lazily under another. A bound name keeps no record of the name it
    came in under, so this reads the lazy objects still waiting, which name
    it as ``<lazy_import 'x.f'>``. So a module that first loads while the name
    it aliases is patched, and binds the alias then, keeps the mock."""

    global _last_read  # noqa: PLW0603 - where the last read left sys.modules
    # A module loads at the end of sys.modules, so an unchanged count and
    # last entry mean none loaded since.
    mark = (len(sys.modules), next(reversed(sys.modules), None))
    if mark == _last_read:
        return
    _last_read = mark
    for key in sys.modules.keys() - _read:
        _read.add(key)
        with contextlib.suppress(TypeError):
            for bound, value in tuple(vars(sys.modules.get(key)).items()):
                if isinstance(value, LazyImportType):
                    imported = repr(value).split("'")[1].rpartition(".")[2]
                    if imported != bound:
                        _aliased.setdefault(imported, set()).add((key, bound))


_markers: tuple[int, Path] | None = None


def own_markers() -> Path:
    """This process's directory of the controller's markers, with their
    settings pointed into it.

    The run's own directory is every worker's, so a heartbeat one worker's
    test wrote would decide another's controller standing as it ran. Keyed by
    process, since a forked worker inherits its parent's.
    """

    global _markers  # noqa: PLW0603 - one directory per process, made on first use
    if _markers is None or _markers[0] != os.getpid():
        directory = Path(tempfile.mkdtemp(prefix="markers-"))
        for setting, name in MARKERS:
            setattr(settings, setting, str(directory / name))
        _markers = (os.getpid(), directory)
    return _markers[1]


def _markers_held() -> dict[str, tuple[bytes, int]]:
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in own_markers().iterdir()}


def _hold_markers(held: dict[str, tuple[bytes, int]]) -> None:
    """Leave exactly ``held`` in the markers directory, each as old as it was."""

    for path in own_markers().iterdir():
        path.unlink(missing_ok=True)
    for name, (content, mtime) in held.items():
        path = own_markers() / name
        path.write_bytes(content)
        os.utime(path, ns=(mtime, mtime))


class CompositionIsolation:
    """Make process caches, and the agent limits and the controller's markers
    every request shares, obey the same per-test isolation as the database.

    The limits and markers are isolated as ``setUpTestData`` rows are: each
    class sets up on a new set, and each of its tests gets a copy of what that
    setup left, so a stream an estate opens or a heartbeat it writes in
    ``setUpClass`` reaches every test of the class and nothing one test takes
    or writes reaches the next.
    """

    _class: type | None = None
    _class_limits: Limits | None = None
    _class_markers: dict[str, tuple[bytes, int]] | None = None

    def startTest(self, test):
        plugins.clear_plugin_composition_cache()
        automation_adoption.forget()
        if type(test) is not self._class:
            # The first of its class: what the set holds is what its setup left.
            self._class, self._class_limits = type(test), agent_limits.LIMITS
            self._class_markers = _markers_held()
        else:
            _hold_markers(self._class_markers or {})
        agent_limits.LIMITS = _copied(self._class_limits)
        return super().startTest(test)

    def stopTest(self, test):
        # The next class sets up on a new set. A new set rather than an
        # emptied one: a call a test left running gives its slot back to the
        # set it took it from.
        agent_limits.LIMITS = agent_limits.Limits()
        _hold_markers({})
        return super().stopTest(test)


def _copied(limits: agent_limits.Limits) -> agent_limits.Limits:
    """A set holding what ``limits`` holds, each count its own and behind a lock of its own."""

    copied = agent_limits.Limits.__new__(agent_limits.Limits)
    with limits._lock:  # noqa: SLF001 - the copy is the set's whole state, which it offers no way to read
        held = vars(limits)
        copied.__dict__.update(
            {name: dict(value) if isinstance(value, dict) else value for name, value in held.items()}
        )
    copied._lock = threading.Lock()  # noqa: SLF001 - a lock of its own, not the original's
    return copied
