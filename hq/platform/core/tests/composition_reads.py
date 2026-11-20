"""Each statement that reads the plugin composition while its module is being imported.

`python -m hq.platform.core.tests.composition_reads <module>...` boots HQ with
imports as written, imports each named module, and prints one JSON line: every
``module:line statement`` whose module body was running when
``composition()``, the one way to the composition, was called. Only the named
modules' top-level packages count as ours. test_composition_at_import.py reads
it.

A read is seen whatever name or call chain led to it: the recorder is a
``sys.monitoring`` callback on the start of ``composition``'s code, installed
before Django or any of HQ loads, so the reads ``django.setup()`` makes are
seen too. A cache derived from the composition is keyed on it, so every read
reaches it, a cached one included.
"""

import importlib
import json
import linecache
import sys
from pathlib import Path
from types import CodeType

PREFIX = "composition-reads "
_PLUGINS = Path(__file__).resolve().parents[2] / "application" / "plugins.py"
_READER = "composition"

_ours: tuple[str, ...] = ()
_found: set[str] = set()


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


def _watch() -> None:
    monitoring = sys.monitoring
    tool = next(tool for tool in range(6) if monitoring.get_tool(tool) is None)
    monitoring.use_tool_id(tool, "composition reads")
    monitoring.register_callback(tool, monitoring.events.PY_START, _seen)
    monitoring.set_events(tool, monitoring.events.PY_START)


def reads(modules: list[str]) -> list[str]:
    global _ours
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
