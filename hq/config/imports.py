"""How every HQ process imports: lazily (PEP 810), so it holds only what its work reached.

Registration is never an import's side effect: it lives in each app's
``ready()`` and Django's autodiscovery. A library that registers as it is
imported is named in ``EAGER``.
"""

import sys
from types import FrameType

# Imported eagerly, with everything they import. Another library that loads while one of them
# does loads whole, so a library one pulls in needs no entry of its own.
EAGER = (
    # Registers Django's own system checks by importing each module that declares some; `check`
    # and the deploy checks run them from the registry.
    "django.core.checks",
    # Registers the date transforms (__year, __date, __hour) on DateField and DateTimeField; a
    # query finds them on the field, and nothing it loads first names this package.
    "django.db.models.functions",
    # Connects the setting_changed receivers that clear what other modules cached from a setting
    # (the storages, the template engines, the URLconf) as it loads; override_settings fires the
    # signal and only names this module for template_rendered, which a test may never use.
    "django.test.signals",
    # Sets __module__ on each name it binds, read from its own namespace as it loads, where a
    # lazy name is not yet what it binds.
    "httpcore2",
    # The test suite's: each strategy registers as its module loads, and the package checks, as it
    # loads, that every one it exports did.
    "hypothesis.strategies",
    # Each generated _pb2 module adds its file to protobuf's descriptor pool as it loads, after the
    # files it imports, which must be in the pool first; the span exporter encodes through them.
    "opentelemetry.proto",
)


def _within(name: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in EAGER)


def _pulled_in(importer: str) -> bool:
    """Whether ``importer`` is another library loading because an ``EAGER`` module is, on this thread.

    The interpreter calls the filter as an import statement runs (PEP 810), so
    each module whose import led here is a ``<module>`` frame up the stack. The
    rest of an entry's own library stays lazy: an entry names the part of it
    that must load at once.
    """

    library = importer.partition(".")[0]
    frame: FrameType | None = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == "<module>":
            name = frame.f_globals.get("__name__", "")
            if _within(name) and name.partition(".")[0] != library:
                return True
        frame = frame.f_back
    return False


def _lazy(importer: str, imported: str, _fromlist: tuple[str, ...] | None = None) -> bool:
    """Whether ``importer``'s import of ``imported`` waits for its first use."""

    return not (_within(importer) or _within(imported) or _pulled_in(importer))


def _as_written(_importer: str, _imported: str, _fromlist: tuple[str, ...] | None = None) -> bool:
    """An explicit ``lazy`` import waits, as its source says; ``eagerly()``'s mark."""

    return True


def lazily() -> None:
    """Every import this process runs from here on waits for its first use, but ``EAGER``'s.

    A process that called ``eagerly()`` keeps its imports as written.
    """

    if sys.get_lazy_imports_filter() is _as_written:
        return
    sys.set_lazy_imports_filter(_lazy)
    sys.set_lazy_imports("all")


def eagerly() -> None:
    """Imports run as written from here on, whatever an entrypoint asks, only an explicit ``lazy`` one waiting.

    For a process that judges what HQ reaches by what it loaded, which a lazy
    import would hide. Explicit ``lazy`` imports still wait, since some break
    an import cycle.
    """

    if sys.get_lazy_imports() == "all":
        raise RuntimeError("imports are lazy already, so modules loaded so far hold lazy names: call eagerly() first")
    sys.set_lazy_imports_filter(_as_written)
