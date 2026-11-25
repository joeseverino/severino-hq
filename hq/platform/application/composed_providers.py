"""An extension's part of a shared composition, derived once per change.

What an extension contributes to a page many domains share (the queue, the
dashboard, the calendar) is asked for on every request that composes one. The
host therefore asks through a derivation of its own, named for the plugin and
the provider: the answer is kept until a table the provider was seen to read is
written, and an extension is cached without knowing a cache exists.

What a provider reads is learned as it runs, the clock included: a day it
reads holds the answer until local midnight, an instant for a minute. It
varies by the one thing a provider is given that is not a row or the clock:
whether the reader asked for a demo.
"""

import hashlib
import inspect
import itertools
import marshal
import pickle
import threading
import time
import weakref
from collections.abc import Callable, Iterable
from dataclasses import is_dataclass, replace
from functools import lru_cache, partial
from types import CodeType
from typing import Any

from .demo import showing_demo
from .derivations import derivation

# The providers answered with one value for everybody, by field.
DERIVED_PROVIDERS = ("attention", "dashboard", "overview")
# How many windows of one calendar source are kept current: the one the
# dashboard shows and the one last paged to.
CALENDAR_WINDOWS = 2

_PROVIDERS: dict[str, Callable[..., Any]] = {}
_ANSWERS: dict[str, Callable[..., Any]] = {}
# Held while a name is declared, so threads asking for a new one at once
# declare it once.
_DECLARING = threading.Lock()


# This process, and each provider met in it that nothing else tells apart.
_PROCESS = time.time_ns()
_UNNAMED: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_SERIAL = itertools.count(1)


def _itself(provider: Any) -> str:
    """A provider known only as the object it is, and only in this process."""

    try:
        # One step, so threads meeting it at once give it one serial.
        serial = _UNNAMED.setdefault(provider, next(_SERIAL))
    except TypeError:
        # Not weakly referable: its address names it while it lives.
        return f"{_PROCESS}:at:{id(provider)}"
    return f"{_PROCESS}:{serial}"


def _holding(values: Any) -> str | None:
    """What a provider was built holding, as a digest; None when it cannot be told."""

    try:
        return hashlib.sha256(pickle.dumps(values)).hexdigest()[:16]
    except Exception:  # noqa: BLE001 - whatever cannot be pickled cannot be compared
        return None


@lru_cache(maxsize=1024)
def _written(code: CodeType) -> str:
    """A function's own code as a digest: two functions of one name differ by it."""

    return hashlib.sha256(marshal.dumps(code)).hexdigest()[:16]


def _identity(provider: Callable[..., Any]) -> str:
    """Which function answers, so one provider is never answered with what
    another stored.

    Named by where it is defined, what it is written to do (which every process
    running one release agrees on) and what it was built holding, so a closure
    made again on every call is the same provider while it holds the same
    things. Anything that cannot be told apart that way is known only as itself.
    """

    if isinstance(provider, partial):
        held = _holding((provider.args, sorted(provider.keywords.items())))
        return _itself(provider) if held is None else f"{_identity(provider.func)}({held})"
    if not inspect.isfunction(provider):
        return _itself(provider)
    name = f"{provider.__module__}:{provider.__qualname__}:{_written(provider.__code__)}"
    if provider.__closure__ is None:
        return name
    try:
        held = _holding(tuple(cell.cell_contents for cell in provider.__closure__))
    except ValueError:
        # A cell not yet filled.
        held = None
    return _itself(provider) if held is None else f"{name}[{held}]"


def answered_by(provider: Callable[..., Any]) -> str:
    """Which function answers when ``provider`` is asked: the one registered
    behind a derived provider, the provider itself otherwise."""

    for name, answer in _ANSWERS.items():
        if answer is provider:
            return _identity(_PROVIDERS[name])
    return _identity(provider)


def _kept(value: Any) -> Any:
    """What a provider returned, as a value that can be stored: a generator's
    items, anything else as it is."""

    if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
        return value
    return tuple(value)


def provided_under(plugin_id: str) -> str:
    """The namespace of every derivation the host asks an extension's
    providers through."""

    return f"extension.{plugin_id}"


def provided(name: str, provider: Callable[..., Any], *, ahead: int | None = None) -> Callable[..., Any]:
    """``provider`` asked through the derivation ``name``, declared on first use.

    The derivation outlives one composition: it answers with whichever
    provider is registered under its name now.
    """

    _PROVIDERS[name] = provider
    answer = _ANSWERS.get(name)
    if answer is not None:
        return answer
    with _DECLARING:
        if (answer := _ANSWERS.get(name)) is not None:
            return answer

        def vary(*args: Any) -> tuple[Any, ...]:
            return (*args, showing_demo(), _identity(_PROVIDERS[name]))

        def compute(*args: Any) -> Any:
            return _kept(_PROVIDERS[name](*args))

        # Asked as the provider is: what ``derive_after_commit`` reads.
        compute.__signature__ = inspect.signature(provider)  # type: ignore[attr-defined]  # a function carries its signature

        declare = (
            derivation(name, reads=(), vary=vary, learns=True)
            if ahead is None
            else derivation(name, reads=(), vary=vary, learns=True, ahead=ahead)
        )
        answer = _ANSWERS[name] = declare(compute)
        return answer


def _is_source(source: Any) -> bool:
    """Whether this is a calendar source to wrap; anything else is the
    calendar's to refuse, by name."""

    return is_dataclass(source) and not isinstance(source, type) and callable(getattr(source, "events", None))


def derived_calendars(plugin_id: str, provider: Callable[[], Iterable[Any]]) -> Callable[[], tuple[Any, ...]]:
    """The provider's sources, each reading its window through a derivation."""

    def derived(source: Any) -> Any:
        if not _is_source(source):
            return source
        events = provided(f"{provided_under(plugin_id)}.calendar.{source.id}", source.events, ahead=CALENDAR_WINDOWS)
        return replace(source, events=events)

    def sources() -> tuple[Any, ...]:
        return tuple(derived(source) for source in provider())

    return sources
