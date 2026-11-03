"""Each statement that reads the plugin composition while its module is being imported.

`python -m hq.platform.core.tests.composition_reads <module>...` boots HQ with
imports as written, imports each named module, and prints one JSON line: every
``module:line statement`` whose module body was running when
``installed_plugins()``, the one way to the composition, was called. Only the
named modules' top-level packages count as ours. test_composition_at_import.py
reads it.

A read is seen whatever name or call chain led to it: the recorder is a
``sys.monitoring`` callback on the start of ``installed_plugins``'s code,
installed before Django or any of HQ loads, so the reads ``django.setup()``
makes are seen too. A derived graph cached on the way would answer a second
module from the first one's read, so ``clear_plugin_composition_cache()``
forgets them as each module of ours starts and finishes executing.
"""

import importlib
import importlib.abc
import json
import linecache
import sys
import threading
from pathlib import Path
from types import CodeType

PREFIX = "composition-reads "
_PLUGINS = Path(__file__).resolve().parents[2] / "application" / "plugins.py"
_READER = "installed_plugins"

_ours: tuple[str, ...] = ()
_found: set[str] = set()
# Held while the clear runs, so the imports the clear makes skip it.
_clearing = threading.Lock()


def _module_of_ours(name: str) -> bool:
    return name != "__main__" and name.partition(".")[0] in _ours


def _seen(code: CodeType, _offset: int) -> object:
    if code.co_qualname != _READER or Path(code.co_filename) != _PLUGINS:
        return sys.monitoring.DISABLE
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if frame.f_code.co_name == "<module>" and _module_of_ours(name):
            line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            _found.add(f"{name}:{frame.f_lineno} {line}")
            break
        frame = frame.f_back
    return None


def _forget() -> None:
    """The composition and every graph cached from it, gone around each module of ours.

    Not while the clear itself runs: its own lazy names load then.
    """

    plugins = sys.modules.get("hq.platform.application.plugins")
    clear = getattr(plugins, "clear_plugin_composition_cache", None)
    if clear is None or not _clearing.acquire(blocking=False):
        return
    try:
        clear()
    finally:
        _clearing.release()


class _Forgetting(importlib.abc.Loader):
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        _forget()
        try:
            self._loader.exec_module(module)
        finally:
            # The module that imported this one goes on reading afresh, too.
            _forget()


class _Finder(importlib.abc.MetaPathFinder):
    """Finds nothing itself: wraps the loader of each module of ours the rest of the path finds."""

    def find_spec(self, fullname, path, target=None):
        if not _module_of_ours(fullname):
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _Forgetting(spec.loader)
                return spec
        return None


def _watch() -> None:
    monitoring = sys.monitoring
    tool = next(tool for tool in range(6) if monitoring.get_tool(tool) is None)
    monitoring.use_tool_id(tool, "composition reads")
    monitoring.register_callback(tool, monitoring.events.PY_START, _seen)
    monitoring.set_events(tool, monitoring.events.PY_START)
    sys.meta_path.insert(0, _Finder())


def reads(modules: list[str]) -> list[str]:
    global _ours  # noqa: PLW0603 - set once, before the first import it judges
    _ours = tuple(sorted({"hq", *(name.partition(".")[0] for name in modules)}))
    _watch()

    from hq.config import imports

    imports.eagerly()
    import django

    django.setup()
    for name in modules:
        importlib.import_module(name)
    return sorted(_found)


if __name__ == "__main__":
    print(PREFIX + json.dumps(reads(sys.argv[1:])))
