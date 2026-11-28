"""What HQ learns, heard by the pages open on it once its answers are stored.

A part of a page drawn from what HQ holds names the read set it is drawn from
(``data-fragment-live``, one of ``reads``), and fragment.js asks for it again
when a write to one of that set's tables commits. Each page holds one stream
(``stream``) that says which sets changed, and nothing else: the part is then
read through its own region, as any refresh is.

The tables written are the ones whose revisions moved (``core.revisions``),
which every writer moves, in any process. The keeper of derived answers reads
them in each pass and tells this hub after it has stored what they change
(``core.ahead``), so a burst of commits is one change and a page asking again
finds its answer stored.

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
import secrets
import threading
from collections import Counter, deque
from collections.abc import AsyncIterator, Iterable, Iterator
from functools import cache
from typing import Any

from django.apps import apps
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

lazy from hq.platform.application.derived_inputs import DASHBOARD_READS, ESTATE_READS, READER_QUEUE_READS
lazy from hq.platform.core import speculation

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


class _Hub:
    """The changes heard, numbered, and the streams waiting for the next."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._kept: deque[tuple[int, frozenset[str]]] = deque(maxlen=KEPT)
        self._waiting: dict[asyncio.Event, asyncio.AbstractEventLoop] = {}
        self._open: Counter[Any] = Counter()

    def told(self, tables: Iterable[str]) -> None:
        """The tables one pass of the keeper found written: one change, for
        every set that reads any of them."""

        sets_of = _sets_of()
        names = frozenset(name for table in tables for name in sets_of.get(table, ()))
        if not names:
            return
        with self._lock:
            self._seq += 1
            self._kept.append((self._seq, names))
            waiting = list(self._waiting.items())
        for event, loop in waiting:
            with contextlib.suppress(RuntimeError):  # a loop that has closed has no stream left
                loop.call_soon_threadsafe(event.set)

    def now(self) -> str:
        """The id of the last change heard: what a page drawn now has seen."""

        with self._lock:
            return f"{BOOT}.{self._seq}"

    def since(self, seen: str) -> tuple[str, frozenset[str]]:
        """The last change's id, and the sets changed after ``seen``: every set
        when ``seen`` is not one this process can answer for."""

        boot, _, number = seen.partition(".")
        with self._lock:
            last = self._seq
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
