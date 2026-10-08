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

A computation this module cannot follow to the clock is ``unseen``: an
installed extension's provider, or a derivation an extension declares. Its
answer stands until what it asked here, and in any case no longer than
``UNSEEN``, nor past the next local midnight. Nothing HQ words is finer than a
minute and a count of days turns at midnight, so such an answer is never a day
late and never more than a minute behind the clock.

An answer a request asked for is asked again as soon as it stops standing:
when a table it reads is written, and when its moment comes. ``derive_ahead``
does that, off any request, in the context the question was first asked in, so
the next request finds its answer stored. The one that computes is the request
that follows its own write.

It fails toward deriving. If the revisions cannot be read, a table has lost its
triggers, the cache cannot be read or a stored value does not load, the
function runs and its answer is used.
"""

import copyreg
import hashlib
import logging
import math
import re
import threading
import time as monotonic_time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import wraps
from types import MappingProxyType
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .projection import projection_scope, read_once, seeded

logger = logging.getLogger("severino.derivations")

# A read-only mapping has no pickle of its own. A stored value carries it as a
# dict and loads it read-only again.
def _read_only(mapping: dict[Any, Any]) -> MappingProxyType[Any, Any]:
    return MappingProxyType(mapping)


copyreg.pickle(MappingProxyType, lambda mapping: (_read_only, (dict(mapping),)))

# The clock, as the module holds it. Derivations reach it only through the
# functions below, which is what lets a test forbid every other way in.
_clock = timezone.now

# The longest a stored value is kept, in seconds; its key outlives its use.
_LONGEST = 24 * 60 * 60
# The longest an answer stands when its computation may read the clock where
# this module cannot see: the finest unit HQ words an age in.
UNSEEN = timedelta(minutes=1)
# How many ways of asking one derivation are asked again ahead of a request
# unless it says otherwise, and how long after a request last asked that way.
REMEMBERED = 8
ASKED_WITHIN = 24 * 60 * 60

# Every declared derivation, by name.
DERIVATIONS: dict[str, Derivation] = {}
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
    # Whether what it reads is learned as it reads, not declared ahead.
    learns: bool = False

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
_AHEAD: ContextVar[bool] = ContextVar("hq_derivation_ahead", default=False)


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
    holds_until(_midnight_after(local))
    return local.date()


def _midnight_after(local: datetime) -> datetime:
    following = datetime.combine(local.date() + timedelta(days=1), time.min)
    return timezone.make_aware(following, timezone.get_current_timezone())


# ----- Declaring and answering -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Derivation:
    name: str
    reads: tuple[str, ...]
    compute: Callable[..., Any]
    vary: Callable[..., Any] | None
    # Whether the computation may read the clock where this module cannot see.
    unseen: bool = False
    # How many ways a request asked it are asked again ahead of the next one.
    ahead: int = REMEMBERED
    _tables: list[frozenset[str]] = field(default_factory=list, compare=False, repr=False)
    # Tables a computation was seen to read beyond ``reads``: an installed
    # extension's, which the host cannot name. Kept for the life of the process.
    _learned: set[str] = field(default_factory=set, compare=False, repr=False)

    @property
    def declared(self) -> frozenset[str]:
        """The database tables behind ``reads``, resolved once models load."""

        if not self._tables:
            from django.apps import apps

            self._tables.append(
                frozenset(apps.get_model(label)._meta.db_table for label in self.reads)
            )
        return self._tables[0]

    @property
    def tables(self) -> frozenset[str]:
        """Every table the answer is keyed on: the declared ones and any a
        computation was seen to read."""

        return self.declared | self._learned if self._learned else self.declared

    def reads_table(self, table: str) -> bool:
        """Whether the answer is keyed on ``table``, asked without building the set."""

        return table in self.declared or table in self._learned

    def learn(self, tables: set[str]) -> bool:
        """Key the answer on ``tables`` too; whether any of them is new."""

        new = tables - self.tables
        self._learned.update(new)
        return bool(new)


def derivation[T](
    name: str,
    *,
    reads: tuple[str, ...],
    vary: Callable[..., Any] | None = None,
    unseen: bool = False,
    ahead: int = REMEMBERED,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Declare a function as a derived fact of the tables in ``reads``.

    ``reads`` names every model the function queries, directly or through
    another derivation, as ``app_label.Model``. ``vary`` takes the call's
    arguments and returns what of them the answer depends on, as something
    with a stable ``repr``; left out, the arguments themselves are used, so
    they must then be plain values.

    ``unseen`` says the function may read the clock where this module cannot
    see, and bounds how long its answer stands (``UNSEEN``). A question a
    request asked is asked again when its answer stops standing; ``ahead`` is
    how many ways of asking are kept current so, the most recently asked
    first, and 0 keeps none.
    """

    def declare(function: Callable[..., T]) -> Callable[..., T]:
        if name in DERIVATIONS:
            raise ImproperlyConfigured(f"Derivation {name!r} is declared twice.")
        declared = Derivation(name, tuple(sorted(set(reads))), function, vary, unseen, ahead)
        DERIVATIONS[name] = declared

        @wraps(function)
        def answer(*args: Any, **kwargs: Any) -> T:
            variant = repr(vary(*args, **kwargs) if vary else (args, sorted(kwargs.items())))
            _may_ask(declared)
            value, until = read_once(
                f"derivation:{name}:{variant}",
                lambda: _asked(declared, answer, variant, args, kwargs),
            )
            # Told on every call, the first in a projection or not: whatever
            # asks reads what this reads and stands no longer than it does.
            _tell(declared, until)
            return value

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


def _may_ask(declared: Derivation) -> None:
    """Refuse a derivation that asks another without declaring what it reads.

    One whose reads are learned, not declared, is held to nothing here: what
    the other reads becomes what it reads.
    """

    parent = _FRAME.get()
    if parent is None or parent.learns or declared.declared <= parent.tables:
        return
    missing = ", ".join(sorted(declared.declared - parent.tables))
    raise ImproperlyConfigured(
        f"A derivation calls {declared.name!r} without declaring what it reads: {missing}."
    )


def _tell(declared: Derivation, until: datetime | None) -> None:
    """Pass what one derivation reads, and how long it stands, to the one asking."""

    parent = _FRAME.get()
    if parent is None:
        return
    parent.undeclared |= declared.tables - parent.tables
    if until is not None:
        parent.hold_until(until)


def _answer(
    declared: Derivation, variant: str, args: tuple, kwargs: dict
) -> tuple[Any, datetime | None]:
    """The answer and the moment it stops standing."""

    key = None if _UNCACHED.get() else _key(declared, variant)
    value, until, found = _stored(key)
    if not found and (_awaited(key) or _awaited_pass(declared, key)):
        # Another thread was deriving this very answer, or was asking again
        # everything that no longer stood: it is stored now.
        value, until, found = _stored(key)
    if not found:
        with _deriving(key), _for_a_request():
            value, until, learned = _compute(declared, args, kwargs)
            if learned and key is not None:
                # Stored under a key that moves with what it was seen to read.
                key = _key(declared, variant)
            found = _keep(key, value, until)
    if found and key is not None:
        # What this projection was answered from, for ``standing``.
        read_once("derivation.answered", dict)[key] = until
    return value, until


# The answers being derived right now, by key, each with the event its
# derivation sets when it has stored. A second thread that wants one waits for
# it, so an answer two requests need at once is derived by one of them.
_IN_FLIGHT: dict[str, threading.Event] = {}
_IN_FLIGHT_LOCK = threading.Lock()
# The longest a thread waits for another's answer before deriving its own, in seconds.
WAITS = 5.0


def _awaited(key: str | None) -> bool:
    """Wait for the thread deriving ``key``, if one is; whether one was.

    Never inside a transaction: this thread may then hold the write lock the
    other needs to store its answer, and would wait on itself.
    """

    from django.db import connection

    if key is None or connection.in_atomic_block:
        return False
    with _IN_FLIGHT_LOCK:
        stored = _IN_FLIGHT.get(key)
    return stored is not None and stored.wait(WAITS)


# Set unless a pass of ``derive_ahead`` is in flight, and set unless a request
# is deriving for itself. Two threads deriving at once each run at half speed,
# so each side waits for the other where it holds nothing the other needs: a
# request at its own top-level question, the pass between two questions.
_NO_PASS = threading.Event()
_NO_PASS.set()
_NO_REQUEST = threading.Event()
_NO_REQUEST.set()
_REQUESTS: list[int] = [0]
# The longest a pass waits between questions for requests to finish, in seconds.
GIVES_WAY = 2.0
# What the pass in flight has stored so far, and the derivations requests are
# waiting on, which the pass asks next. Both are read and written under the
# condition, which is notified as each question is answered.
_TURN = threading.Condition()
_PASS_STORED: set[str] = set()
_WANTED: Counter[str] = Counter()


def _awaited_pass(declared: Derivation, key: str | None) -> bool:
    """Wait for the pass in flight, if one is, to store what this request asks.

    Only at a request's own top-level question, where it holds nothing the
    pass could be waiting for, and never inside a transaction. The pass asks
    what is waited on first, and the wait ends when that answer is stored.
    """

    from django.db import connection

    if key is None or _AHEAD.get() or _FRAME.get() is not None or _NO_PASS.is_set():
        return False
    if connection.in_atomic_block:
        return False
    deadline = monotonic_time.monotonic() + WAITS
    with _TURN:
        _WANTED[declared.name] += 1
        try:
            while not _NO_PASS.is_set() and key not in _PASS_STORED:
                remaining = deadline - monotonic_time.monotonic()
                if remaining <= 0:
                    break
                _TURN.wait(remaining)
        finally:
            _WANTED[declared.name] -= 1
    return True


def _next_asked(pending: list[tuple[str, str, _Ask]]) -> int:
    """Which pending question the pass asks next: one a request waits on, else the first."""

    with _TURN:
        for index, (name, _variant, _ask) in enumerate(pending):
            if _WANTED[name] > 0:
                return index
    return 0


@contextmanager
def _for_a_request() -> Iterator[None]:
    """Say a request is deriving for itself, so a pass waits its turn."""

    if _AHEAD.get():
        yield
        return
    with _IN_FLIGHT_LOCK:
        _REQUESTS[0] += 1
        _NO_REQUEST.clear()
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _REQUESTS[0] -= 1
            if _REQUESTS[0] == 0:
                _NO_REQUEST.set()


@contextmanager
def _deriving(key: str | None) -> Iterator[None]:
    """Say that ``key`` is being derived, until it has been stored or failed."""

    stored = threading.Event()
    with _IN_FLIGHT_LOCK:
        mine = key is not None and _IN_FLIGHT.setdefault(key, stored) is stored
    try:
        yield
    finally:
        if mine and key is not None:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.pop(key, None)
            stored.set()


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
    except Exception:  # an unreadable cache is a miss, never an error page
        logger.exception("The derived cache could not be read; deriving instead.")
        return None, None, False
    if entry is None:
        return None, None, False
    value, until = entry
    if until is not None and _clock() >= until:
        return None, None, False
    SERVED[key.partition(":")[0]] += 1
    return value, until, True


def _compute(declared: Derivation, args: tuple, kwargs: dict) -> tuple[Any, datetime | None, bool]:
    """The value, how long it holds, and whether computing it showed a table
    its key did not yet move with.

    A computation is a function of the tables it read. Whatever made it read
    one more was itself a change to a table it had read, so the answer stored
    before was sound, and from here its key moves with the new table too.
    ``reads`` is the floor the host states; an installed extension's tables,
    which the host cannot name, are learned the first time they are read.
    """

    from django.db import connection

    frame = _Frame(declared.tables, learns=declared.unseen)
    token = _FRAME.set(frame)
    try:
        # What an unseen computation reads is learned from the statements it
        # runs, so it runs in a projection of its own: a read another part of
        # the request already made is made again here, where it is seen.
        apart = projection_scope(seeded(), apart=True) if declared.unseen else nullcontext()
        with apart, connection.execute_wrapper(frame.note):
            value = declared.compute(*args, **kwargs)
    finally:
        _FRAME.reset(token)
    COMPUTED[declared.name] += 1
    if declared.unseen:
        local = timezone.localtime(_clock())
        frame.hold_until(min(local + UNSEEN, _midnight_after(local)))
    learned = declared.learn(frame.undeclared)
    if learned:
        UNDECLARED.setdefault(declared.name, set()).update(frame.undeclared)
        logger.info(
            "Derivation %s also reads %s; its answers are keyed on them from now on.",
            declared.name,
            ", ".join(sorted(frame.undeclared)),
        )
    return value, frame.until, learned


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
    except Exception:  # a value that cannot be stored is still the answer
        logger.exception("A derived value could not be stored.")
        return False
    return True


@dataclass(frozen=True, slots=True)
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


# ----- Asking again, ahead of the next request ----------------------------------


@dataclass
class _Ask:
    """One way a request asked a derivation, kept so it can be asked again."""

    answer: Callable[..., Any]
    args: tuple
    kwargs: dict
    # Everything ambient the question was asked in, and what its projection
    # was seeded with: who is reading, how they reached HQ, a demo or not.
    context: Context
    seed: dict[str, Any]
    asked: float
    # The moment the last answer stops standing; None when it names none.
    until: datetime | None = None


# Every remembered question, by derivation and by what it varied on.
_ASKS: dict[str, dict[str, _Ask]] = {}
_ASKS_LOCK = threading.Lock()


def asked_ahead() -> bool:
    """Whether the question in progress is asked on the next request's behalf.

    An input that says whether anybody is using HQ reads this: the request
    being answered ahead is itself that use.
    """

    return _AHEAD.get()


def _asked(
    declared: Derivation, answer: Callable[..., Any], variant: str, args: tuple, kwargs: dict
) -> tuple[Any, datetime | None]:
    """Answer, and remember how a request asked."""

    value, until = _answer(declared, variant, args, kwargs)
    if declared.ahead and _FRAME.get() is None and not (_UNCACHED.get() or _AHEAD.get()):
        _remember(declared, variant, answer, args, kwargs, _answered_until(declared, variant))
    return value, until


def _answered_until(declared: Derivation, variant: str) -> datetime | None:
    """The moment the answer this projection was just given stops standing."""

    key = _key(declared, variant)
    return read_once("derivation.answered", dict).get(key) if key else None


def _remember(
    declared: Derivation, variant: str, answer: Callable[..., Any], args: tuple, kwargs: dict,
    until: datetime | None,
) -> None:
    now = monotonic_time.monotonic()
    with _ASKS_LOCK:
        asks = _ASKS.setdefault(declared.name, {})
        asks.pop(variant, None)
        context = copy_context()
        # The projection it was asked in is not kept: it is asked again in one
        # of its own, from the same seed.
        context.run(_FRAME.set, None)
        asks[variant] = _Ask(answer, args, kwargs, context, seeded(), now, until)
        while len(asks) > declared.ahead:
            asks.pop(next(iter(asks)))


def forget() -> None:
    """Remember no question. For a test, and for a composition that changed."""

    with _ASKS_LOCK:
        _ASKS.clear()


def _remembered() -> list[tuple[str, str, _Ask]]:
    """Every question still worth asking again, the stale ones forgotten."""

    horizon = monotonic_time.monotonic() - ASKED_WITHIN
    with _ASKS_LOCK:
        for asks in _ASKS.values():
            for variant in [variant for variant, ask in asks.items() if ask.asked < horizon]:
                del asks[variant]
        return [(name, variant, ask) for name, asks in _ASKS.items() for variant, ask in asks.items()]


def derive_ahead(more: Callable[[], bool] | None = None) -> list[str]:
    """Ask again every remembered question whose answer no longer stands.

    Returns the derivations asked. One that fails is forgotten and logged: the
    next request asks it afresh and is told what went wrong. ``more`` says
    whether something was written while this was asking; while it does, the
    questions are gone through again before a waiting request is let go.
    """

    asked: list[str] = []
    _NO_PASS.clear()
    try:
        while True:
            asked += _pass()
            if more is None or not more():
                break
    finally:
        with _TURN:
            _PASS_STORED.clear()
            _NO_PASS.set()
            _TURN.notify_all()
    return asked


def _pass() -> list[str]:
    """Go through the remembered questions once; the derivations asked."""

    asked: list[str] = []
    pending = _remembered()
    while pending:
        # A request deriving for itself goes first: what it stores, this pass
        # then finds standing.
        _NO_REQUEST.wait(GIVES_WAY)
        name, variant, ask = pending.pop(_next_asked(pending))
        try:
            derived, key = ask.context.copy().run(_again, ask)
        except Exception:  # one question failing must not stop the rest
            logger.exception("Derivation %s could not be asked again ahead.", name)
            with _ASKS_LOCK:
                _ASKS.get(name, {}).pop(variant, None)
            continue
        if derived:
            asked.append(name)
        with _TURN:
            _PASS_STORED.add(key)
            _TURN.notify_all()
    return asked


def _again(ask: _Ask) -> tuple[bool, str]:
    """Ask once more as it was first asked: whether there was anything to
    derive, and the key the answer stands under ("" when none is kept)."""

    declared: Derivation = ask.answer.derivation  # type: ignore[attr-defined]
    token = _AHEAD.set(True)
    try:
        with projection_scope(ask.seed, apart=True):
            variant = repr(
                declared.vary(*ask.args, **ask.kwargs)
                if declared.vary
                else (ask.args, sorted(ask.kwargs.items()))
            )
            key = _key(declared, variant)
            if key is None:
                # Nothing is kept while the revisions are not: deriving ahead
                # would store nothing a request could be answered from.
                return False, ""
            if _standing_stored(key):
                if ask.until is not None and ask.until <= _clock():
                    # Derived since by a request: learn how long that stands.
                    ask.until = _stored(key)[1]
                return False, key
            ask.answer(*ask.args, **ask.kwargs)
            kept = _key(declared, variant)
            answered = read_once("derivation.answered", dict)
            if kept is None or kept not in answered:
                raise LookupError(f"The answer of {declared.name} could not be kept.")
            ask.until = answered[kept]
            return True, kept
    finally:
        _AHEAD.reset(token)


def _standing_stored(key: str) -> bool:
    try:
        return bool(_store().has_key(key))
    except Exception:  # noqa: BLE001 - an unreadable cache is a miss
        return False


def asked_of(table: str) -> bool:
    """Whether a remembered question reads ``table``, so a write to it is news."""

    with _ASKS_LOCK:
        asked = [name for name, asks in _ASKS.items() if asks]
    return any(
        (declared := DERIVATIONS.get(name)) is not None and declared.reads_table(table)
        for name in asked
    )


def next_due() -> datetime | None:
    """The earliest moment a remembered answer stops standing; None when none does."""

    moments = [ask.until for _name, _variant, ask in _remembered() if ask.until is not None]
    return min(moments) if moments else None


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
