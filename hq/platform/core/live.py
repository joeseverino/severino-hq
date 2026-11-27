"""What HQ learns, heard by the pages open on it the moment it commits.

A part of a page drawn from what HQ holds names the read set it is drawn from
(``data-fragment-live``, one of ``READS``), and fragment.js asks for it again
when a write to one of that set's tables commits. Each page holds one stream
(``stream``) that says which sets changed, and nothing else: the part is then
read through its own region, as any refresh is.

Every writer commits in this one process (the web and the bridges beside it,
``hq/config/asgi.py``), so a commit is heard here, through the one watch on
what a statement writes, which the derived answers kept ahead hear too
(``core.ahead``).

Security:

- A pulse names read sets, never a record, a kind or a value. Each set is
  read under READ, so a pulse says only that something a page the person may
  open is drawn from changed, which that page's own fetch would show.
- The stream answers a signed-in person holding READ, through the same
  middleware as every page, and a GET with no effect: a speculative request
  is refused it.
- A person holds at most ``STREAMS_PER_PERSON`` streams; a hidden tab holds
  none, and each stream ends after ``LASTS`` so its next one crosses the
  session's checks again.
"""

import asyncio
import contextlib
import logging
import re
import secrets
import threading
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from functools import cache, partial
from typing import Any

from django.apps import apps
from django.db import connections, transaction
from django.db.backends.signals import connection_created
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

lazy from hq.platform.application.derived_inputs import DASHBOARD_READS, ESTATE_READS, READER_QUEUE_READS
lazy from hq.platform.core import speculation

logger = logging.getLogger("severino.live")

# The table a statement writes, as Django and this repository's own SQL quote it.
WRITTEN = re.compile(
    r'\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"(\w+)"',
    re.IGNORECASE,
)

# How long a stream lasts before the browser opens the next, in seconds.
LASTS = 15 * 60
# How often a quiet stream says it is still open, in seconds: inside the
# proxy's read timeout (nginx's proxy_read_timeout, 90 s in front of HQ), and
# carrying the last change's id, since WebKit keeps an id only from an event
# that has data.
BEAT = 30
# How soon the browser opens a stream again once one ends, in milliseconds.
RETRY_MS = 2000
# How many streams one person may hold at once: a visible tab holds one.
STREAMS_PER_PERSON = 16
# How many changes are kept for a stream that comes back, before it is told
# that everything may have changed.
KEPT = 512

# The changes heard in this process are numbered from its start; an id from
# another start says nothing of what this one heard.
BOOT = secrets.token_hex(4)


def _reads() -> dict[str, tuple[str, ...]]:
    return {
        "estate": (*ESTATE_READS, "control_plane.InboundEvent"),
        "queue": READER_QUEUE_READS,
        "dashboard": (*DASHBOARD_READS, "control_plane.DashboardRefreshRequest"),
        "run": (
            "jobs.Job",
            "control_plane.OperationRequest",
            "control_plane.OperationHost",
            "control_plane.InboundEvent",
            "core.AuditLog",
        ),
    }


@cache
def reads() -> dict[str, tuple[str, ...]]:
    """What each part of a page can be drawn from, by the name it declares: model labels."""

    return _reads()


@cache
def _sets_of() -> dict[str, frozenset[str]]:
    """Each table a read set reads, to the sets that read it."""

    found: dict[str, set[str]] = {}
    for name, labels in reads().items():
        for label in labels:
            found.setdefault(apps.get_model(label)._meta.db_table, set()).add(name)
    return {table: frozenset(names) for table, names in found.items()}


def region(name: str, drawn: bool = True) -> dict[str, str]:
    """The ``data-`` hooks of a part of a page drawn from the read set
    ``name`` while ``drawn``: a region of its page, asked for again when a
    change to it commits; none once the server stops drawing them."""

    if name not in reads():
        raise ValueError(f"{name!r} is no read set: {', '.join(reads())}.")
    return {"fragment": "", "fragment-live": name} if drawn else {}


# What hears each table written once its transaction commits.
_listeners: list[Callable[[str], None]] = []
_listening = threading.Lock()


def _tell(table: str) -> None:
    for listener in tuple(_listeners):
        listener(table)


def noted(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]) -> Any:
    """Run the statement; if it wrote a table, say so when its transaction commits."""

    result = execute(sql, params, many, context)
    found = WRITTEN.match(sql)
    if found is not None:
        told = partial(_tell, found[1])
        try:
            transaction.on_commit(told, using=context["connection"].alias, robust=True)
        except transaction.TransactionManagementError:
            # Autocommit is off and no atomic block is open: the commit is the
            # caller's to make, and a reader reads only what has committed.
            told()
    return result


def _watch(connection: Any, **kwargs: Any) -> None:
    # First in the list, not last: ``connection.execute_wrapper()`` removes the
    # last one when its block ends, and a connection can open inside one.
    if noted not in connection.execute_wrappers:
        connection.execute_wrappers.insert(0, noted)


def _unwatch() -> None:
    connection_created.disconnect(dispatch_uid="hq.live")
    for connection in connections.all(initialized_only=True):
        if noted in connection.execute_wrappers:
            connection.execute_wrappers.remove(noted)


@contextlib.contextmanager
def heard(listener: Callable[[str], None]) -> Iterator[None]:
    """``listener`` told each table a committed transaction wrote, until the block ends.

    Every connection is watched while anything listens.
    """

    with _listening:
        if not _listeners:
            connection_created.connect(_watch, dispatch_uid="hq.live", weak=False)
            for connection in connections.all(initialized_only=True):
                _watch(connection)
        _listeners.append(listener)
    try:
        yield
    finally:
        with _listening:
            _listeners.remove(listener)
            if not _listeners:
                _unwatch()


class _Hub:
    """The changes heard, numbered, and the streams waiting for the next."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._kept: deque[tuple[int, frozenset[str]]] = deque(maxlen=KEPT)
        # Heard since the last change was numbered: a burst of writes is one change.
        self._pending: set[str] = set()
        self._waiting: dict[asyncio.Event, asyncio.AbstractEventLoop] = {}
        self._open: Counter[Any] = Counter()

    def told(self, table: str) -> None:
        names = _sets_of().get(table)
        if not names:
            return
        with self._lock:
            woken = not self._pending
            self._pending |= names
            waiting = list(self._waiting.items()) if woken else []
        for event, loop in waiting:
            with contextlib.suppress(RuntimeError):  # a loop that has closed has no stream left
                loop.call_soon_threadsafe(event.set)

    def _numbered(self) -> int:
        if self._pending:
            self._seq += 1
            self._kept.append((self._seq, frozenset(self._pending)))
            self._pending.clear()
        return self._seq

    def now(self) -> str:
        """The id of the last change heard: what a page drawn now has seen."""

        with self._lock:
            return f"{BOOT}.{self._numbered()}"

    def since(self, seen: str) -> tuple[str, frozenset[str]]:
        """The last change's id, and the sets changed after ``seen``: every set
        when ``seen`` is not one this process can answer for."""

        boot, _, number = seen.partition(".")
        with self._lock:
            last = self._numbered()
            after = int(number) if boot == BOOT and number.isdigit() else -1
            oldest = self._kept[0][0] if self._kept else last + 1
            if not 0 <= after <= last or after < oldest - 1:
                changed = frozenset(reads())
            else:
                changed = frozenset(name for seq, names in self._kept if seq > after for name in names)
        return f"{BOOT}.{last}", changed

    def admit(self, person: Any) -> bool:
        with self._lock:
            if self._open[person] >= STREAMS_PER_PERSON:
                return False
            self._open[person] += 1
            return True

    def leave(self, person: Any) -> None:
        with self._lock:
            self._open[person] -= 1
            if self._open[person] <= 0:
                del self._open[person]

    @contextlib.contextmanager
    def waiting(self) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        with self._lock:
            self._waiting[event] = asyncio.get_running_loop()
        try:
            yield event
        finally:
            with self._lock:
                self._waiting.pop(event, None)


HUB = _Hub()


def now() -> str:
    return HUB.now()


@contextlib.contextmanager
def listening() -> Iterator[None]:
    """The streams told what commits, for as long as the web application runs."""

    with heard(HUB.told):
        yield


def said(last: str, changed: Iterable[str]) -> str:
    """One event of the stream: the sets changed, or, with none, that it is still open."""

    names = " ".join(sorted(changed))
    return f"id: {last}\ndata: {names}\n\n" if names else f"event: beat\nid: {last}\ndata:\n\n"


async def _pulses(seen: str) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    ends = loop.time() + LASTS
    with HUB.waiting() as woken:
        yield f"retry: {RETRY_MS}\n\n"
        while True:
            woken.clear()
            seen, changed = HUB.since(seen)
            yield said(seen, changed)
            left = ends - loop.time()
            if left <= 0:
                return
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(BEAT, left)):
                    await woken.wait()


class _Held:
    """One person's stream, given back once its answer closes, however it ended.

    Django closes a streaming answer through its ``close`` whether it was sent
    whole or its client went away before the first event.
    """

    def __init__(self, seen: str, person: Any) -> None:
        self._seen = seen
        self._person = person
        self._closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        return _pulses(self._seen)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            HUB.leave(self._person)


@require_GET
async def stream(request: HttpRequest) -> HttpResponse:
    """The page's stream: an event naming the read sets that changed, from
    the change the page last heard (``Last-Event-ID``, or ``after`` when it
    opens one afresh)."""

    speculation.refuse("holds a stream open")
    person = (await request.auser()).pk
    if not HUB.admit(person):
        refused = HttpResponse(status=429)
        refused["Retry-After"] = str(LASTS)
        return refused
    seen = request.headers.get("Last-Event-ID") or request.GET.get("after", "")
    response = StreamingHttpResponse(_Held(seen, person), content_type="text/event-stream")
    response["Cache-Control"] = "no-store"
    # nginx passes each event on as it is sent rather than holding the answer.
    response["X-Accel-Buffering"] = "no"
    return response
