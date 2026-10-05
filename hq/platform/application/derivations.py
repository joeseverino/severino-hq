"""Derived facts, computed once per change of what they read.

A derivation is a function of three things: the rows of the tables it reads,
its arguments, and the clock. It is declared once::

    @derivation("services.catalog", reads=("control_plane.ManagedResource", ...))
    def service_catalog(favorites=()): ...

and from then on a call is answered in this order:

1. from the projection in progress, so one request never derives twice;
2. from the ``derived`` cache, under a key made of the derivation's name, its
   arguments and the revision of every table it reads;
3. by running the function, and storing what it returned under that key.

The revisions are kept by the database on every write
(``hq.platform.core.revisions``), in the writing transaction, so a key names
exactly one state of the tables. The cache is a database table: a value stored
inside a transaction commits or rolls back with the rows it was derived from.

The clock is an input too. A derivation never reads it directly: it asks
``reached``, ``passed``, ``since``, ``whole`` or ``today`` here, and each answer also
records the moment it stops being true. A stored value is good until the
earliest such moment and is derived again after it, so a threshold is crossed
on time and no age is served stale.

It fails toward deriving. If the revisions cannot be read, a table has lost its
triggers, the cache cannot be read or a stored value does not load, the
function runs and its answer is used.
"""

from __future__ import annotations

import copyreg
import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import wraps
from types import MappingProxyType
from typing import Any, TypeVar

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .projection import read_once

logger = logging.getLogger("severino.derivations")

# A read-only mapping has no pickle of its own. A stored value carries it as a
# dict and loads it read-only again.
def _read_only(mapping: dict[Any, Any]) -> MappingProxyType[Any, Any]:
    return MappingProxyType(mapping)


copyreg.pickle(MappingProxyType, lambda mapping: (_read_only, (dict(mapping),)))

_T = TypeVar("_T")
# The clock, as the module holds it. Derivations reach it only through the
# functions below, which is what lets a test forbid every other way in.
_clock = timezone.now

# The longest a stored value is kept, in seconds; its key outlives its use.
_LONGEST = 24 * 60 * 60

# Every declared derivation, by name.
DERIVATIONS: dict[str, "Derivation"] = {}
# How many times each derivation ran its function, and was answered from the
# cache, in this process. Read by the budgets and the bench.
COMPUTED: Counter[str] = Counter()
SERVED: Counter[str] = Counter()


@dataclass
class _Frame:
    """One derivation in progress: what it may read and how long it holds."""

    tables: frozenset[str]
    until: datetime | None = None
    undeclared: set[str] = field(default_factory=set)

    def note(self, execute: Callable, sql: str, params: Any, many: bool, context: dict) -> Any:
        """Record a table read that the derivation did not declare."""

        for table in _TABLES_READ.findall(sql):
            if table not in self.tables and table not in _bookkeeping():
                self.undeclared.add(table)
        return execute(sql, params, many, context)

    def hold_until(self, moment: datetime) -> None:
        if self.until is None or moment < self.until:
            self.until = moment


# The tables a statement reads, as Django and this repository's own SQL quote them.
_TABLES_READ = re.compile(r'(?:FROM|JOIN)\s+"(\w+)"')
# Read by a derivation that never declared them, by derivation name.
UNDECLARED: dict[str, set[str]] = {}


def _bookkeeping() -> frozenset[str]:
    """The cache and revision tables, which every derivation touches."""

    if not _BOOKKEEPING:
        from hq.platform.core.revisions import _uncounted

        _BOOKKEEPING.append(frozenset(_uncounted()))
    return _BOOKKEEPING[0]


_BOOKKEEPING: list[frozenset[str]] = []
_FRAME: ContextVar[_Frame | None] = ContextVar("hq_derivation_frame", default=None)
_UNCACHED: ContextVar[bool] = ContextVar("hq_derivation_uncached", default=False)


# ----- The clock ---------------------------------------------------------------


def holds_until(moment: datetime) -> None:
    """Say the derivation in progress stops being true at ``moment``."""

    frame = _FRAME.get()
    if frame is not None:
        frame.hold_until(moment)


def reached(moment: datetime, *, now: datetime | None = None) -> bool:
    """Whether ``moment`` has come. False now turns true at ``moment``."""

    current = now or _clock()
    if current >= moment:
        return True
    holds_until(moment)
    return False


def passed(moment: datetime, *, now: datetime | None = None) -> bool:
    """Whether ``moment`` is behind us. False now turns true just after it."""

    current = now or _clock()
    if current > moment:
        return True
    holds_until(moment)
    return False


def since(moment: datetime, *, now: datetime | None = None) -> timedelta:
    """How long ago ``moment`` was, for wording as an age.

    An age is written to one unit: minutes inside the hour and no finer than
    hours after it. The wording therefore holds until the next whole minute of
    age, or the next whole hour once it is an hour old. A threshold is asked
    of ``reached`` or ``passed``, never of this.
    """

    current = now or _clock()
    age = current - moment
    unit = timedelta(minutes=1) if abs(age) < timedelta(hours=1) else timedelta(hours=1)
    holds_until(moment + (math.floor(age / unit) + 1) * unit)
    return age


def present() -> datetime:
    """The moment a derivation is made at, to hand to the functions here as ``now``.

    Held by a rule set so every rule reads one instant. It records nothing:
    whatever is compared against it goes through ``reached``, ``passed``,
    ``since``, ``whole`` or ``expiry.days_until``.
    """

    return _clock()


def whole(since: datetime, unit: timedelta, *, now: datetime | None = None) -> int:
    """Whole ``unit``s elapsed since ``since``; negative before it."""

    current = now or _clock()
    count = math.floor((current - since) / unit)
    holds_until(since + (count + 1) * unit)
    return count


def today() -> date:
    """The local day, which holds until the next local midnight."""

    local = timezone.localtime(_clock())
    following = datetime.combine(local.date() + timedelta(days=1), time.min)
    holds_until(timezone.make_aware(following, timezone.get_current_timezone()))
    return local.date()


# ----- Declaring and answering -------------------------------------------------


@dataclass(frozen=True)
class Derivation:
    name: str
    reads: tuple[str, ...]
    compute: Callable[..., Any]
    vary: Callable[..., Any] | None
    _tables: list[frozenset[str]] = field(default_factory=list, compare=False, repr=False)

    @property
    def tables(self) -> frozenset[str]:
        """The database tables behind ``reads``, resolved once models load."""

        if not self._tables:
            from django.apps import apps

            self._tables.append(
                frozenset(apps.get_model(label)._meta.db_table for label in self.reads)
            )
        return self._tables[0]


def derivation(
    name: str, *, reads: tuple[str, ...], vary: Callable[..., Any] | None = None
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Declare a function as a derived fact of the tables in ``reads``.

    ``reads`` names every model the function queries, directly or through
    another derivation, as ``app_label.Model``. ``vary`` takes the call's
    arguments and returns what of them the answer depends on, as something
    with a stable ``repr``; left out, the arguments themselves are used, so
    they must then be plain values.
    """

    def declare(function: Callable[..., _T]) -> Callable[..., _T]:
        if name in DERIVATIONS:
            raise ImproperlyConfigured(f"Derivation {name!r} is declared twice.")
        declared = Derivation(name, tuple(sorted(set(reads))), function, vary)
        DERIVATIONS[name] = declared

        @wraps(function)
        def answer(*args: Any, **kwargs: Any) -> _T:
            variant = repr(vary(*args, **kwargs) if vary else (args, sorted(kwargs.items())))
            return read_once(
                f"derivation:{name}:{variant}",
                lambda: _answer(declared, variant, args, kwargs),
            )

        answer.derivation = declared  # type: ignore[attr-defined]
        return answer

    return declare


def table_revisions(tables: frozenset[str] | tuple[str, ...]) -> tuple[int, ...] | None:
    """The revisions of ``tables``, read once per projection; None when not kept."""

    from hq.platform.core.revisions import read

    found = read_once("derivation.revisions", read)
    return None if found is None else found.of(sorted(tables))


def every_revision() -> str | None:
    """One key that moves when any counted table is written; None when not kept.

    For an answer assembled from more tables than it is worth naming: it is
    "unchanged" only while nothing at all was written.
    """

    from hq.platform.core.revisions import read

    found = read_once("derivation.revisions", read)
    if found is None:
        return None
    counts = found.of(sorted(found.counts))
    return None if counts is None else ".".join(map(str, counts))


def _store():
    from django.core.cache import caches

    from hq.platform.core.revisions import CACHE_ALIAS

    return caches[CACHE_ALIAS]


def _answer(declared: Derivation, variant: str, args: tuple, kwargs: dict) -> Any:
    parent = _FRAME.get()
    if parent is not None and not declared.tables <= parent.tables:
        missing = ", ".join(sorted(declared.tables - parent.tables))
        raise ImproperlyConfigured(
            f"A derivation calls {declared.name!r} without declaring what it reads: {missing}."
        )
    key = None if _UNCACHED.get() else _key(declared, variant)
    value, until, found = _stored(key)
    if not found:
        value, until = _compute(declared, args, kwargs)
        found = _keep(key, value, until)
    if found and key is not None:
        # What this projection was answered from, for ``standing``.
        read_once("derivation.answered", dict)[key] = until
    if parent is not None and until is not None:
        parent.hold_until(until)
    return value


def _key(declared: Derivation, variant: str) -> str | None:
    """The cache key for this call at the tables' current revisions."""

    found = table_revisions(declared.tables)
    if found is None:
        return None
    digest = hashlib.sha256(f"{variant}|{found}".encode()).hexdigest()
    return f"{declared.name}:{digest}"


def _stored(key: str | None) -> tuple[Any, datetime | None, bool]:
    if key is None:
        return None, None, False
    try:
        entry = _store().get(key)
    except Exception:  # noqa: BLE001 - an unreadable cache is a miss, never an error page
        logger.exception("The derived cache could not be read; deriving instead.")
        return None, None, False
    if entry is None:
        return None, None, False
    value, until = entry
    if until is not None and _clock() >= until:
        return None, None, False
    SERVED[key.partition(":")[0]] += 1
    return value, until, True


def _compute(declared: Derivation, args: tuple, kwargs: dict) -> tuple[Any, datetime | None]:
    from django.db import connection

    frame = _Frame(declared.tables)
    token = _FRAME.set(frame)
    try:
        with connection.execute_wrapper(frame.note):
            value = declared.compute(*args, **kwargs)
    finally:
        _FRAME.reset(token)
    COMPUTED[declared.name] += 1
    if frame.undeclared:
        # Its key would not move when those tables change: never stored.
        UNDECLARED.setdefault(declared.name, set()).update(frame.undeclared)
        logger.error(
            "Derivation %s reads %s without declaring it; not stored.",
            declared.name,
            ", ".join(sorted(frame.undeclared)),
        )
        return value, _clock()
    return value, frame.until


def _keep(key: str | None, value: Any, until: datetime | None) -> bool:
    """Store the value under its key; whether it is there to be served again."""

    if key is None:
        return False
    lifetime: float = _LONGEST
    if until is not None:
        lifetime = min((until - _clock()).total_seconds(), _LONGEST)
        if lifetime <= 0:
            return False
    try:
        _store().set(key, (value, until), timeout=lifetime)
    except Exception:  # noqa: BLE001 - a value that cannot be stored is still the answer
        logger.exception("A derived value could not be stored.")
        return False
    return True


@dataclass(frozen=True)
class Standing:
    """What an answer was derived at, and the moment it stops holding."""

    key: str
    until: datetime | None


def standing_key(function: Callable[..., Any], *args: Any) -> str | None:
    """The key this call is answered under now; None when that cannot be known.

    It moves whenever a table the derivation reads is written or the arguments
    it varies by change. None when ``function`` is not a derivation or its
    revisions are not kept: a caller then computes, and never reads silence as
    "unchanged".
    """

    declared = getattr(function, "derivation", None)
    if declared is None:
        return None
    return _key(declared, repr(declared.vary(*args) if declared.vary else (args, [])))


def standing(function: Callable[..., Any], *args: Any) -> Standing | None:
    """The key and the expiry of the answer this projection was just given.

    None unless the call was answered in the projection in progress from a
    stored value, or stored by it.
    """

    key = standing_key(function, *args)
    answered = read_once("derivation.answered", dict)
    if key is None or key not in answered:
        return None
    return Standing(key, answered[key])


@contextmanager
def uncached() -> Iterator[None]:
    """Derive inside the block, reading and storing nothing.

    For measuring what a derivation itself costs: a budget on its queries, a
    profile of its work.
    """

    token = _UNCACHED.set(True)
    try:
        yield
    finally:
        _UNCACHED.reset(token)


@contextmanager
def counting() -> Iterator[tuple[Counter[str], Counter[str]]]:
    """What ran and what was served from the cache inside the block."""

    ran: Counter[str] = Counter()
    served: Counter[str] = Counter()
    before_ran, before_served = Counter(COMPUTED), Counter(SERVED)
    try:
        yield ran, served
    finally:
        ran.update(COMPUTED - before_ran)
        served.update(SERVED - before_served)
