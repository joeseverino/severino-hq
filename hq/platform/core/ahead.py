"""Derived answers kept current for as long as the web application runs.

A derived answer stops standing when a table it reads is written or when its
moment comes (``hq.platform.application.derivations``). Left there, the next
request pays for deriving it again, and with a controller reporting every few
minutes while HQ is in use, many requests a person makes are that next request.

So the question is asked again when the change lands. Every statement a
connection runs is seen here; one that writes a table a remembered question
reads says so once its transaction commits, and one thread then asks again
whatever no longer stands. It never runs inside the writing transaction or the
writer's thread, and a rolled-back write says nothing. The same thread wakes
when the earliest remembered answer's moment comes.

Writes that land together are one change: the pass starts once none has landed
for a moment. Writes that land while it is deriving are answered by one more
pass, not one each: a pass asks only what does not stand at the revisions it
reads, so a question is derived at most once per state of its tables. While
writes keep landing (an import), what a pass derives is unsettled before it is
read, so each pass waits twice as long for them to settle, up to
``SETTLES_LONGEST``, and a request derives what it asks for itself, as it would
with no pass at all. The first pass that nothing interrupts restores the wait.

A request never waits on the pass: it derives what it needs itself, and the
pass later finds that answer stored. The pass pauses between questions while a
request is deriving. Only a request needing an answer another thread is
deriving waits, for that answer.

Nothing here decides what is true. A request that arrives before the thread
has finished derives for itself, exactly as it did before.
"""

import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic
from typing import Any

from django.db import close_old_connections, connections, transaction
from django.db.backends.signals import connection_created
from django.utils import timezone

from hq.platform.application import derivations

logger = logging.getLogger("severino.ahead")

# The table a statement writes, as Django and this repository's own SQL quote it.
_WRITTEN = re.compile(
    r'\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"(\w+)"',
    re.IGNORECASE,
)
# A pass started by an answer's moment is no closer than this to the last one,
# in seconds, so an answer that cannot be kept is not derived in a loop.
RESTS = 1.0
# Writes that land together are one change: a pass starts once none has landed
# for this long, in seconds, and no later than ``SETTLES_WITHIN`` after the first.
SETTLES = 0.05
SETTLES_WITHIN = 0.5
# The longest that wait grows to while writes keep landing during passes.
SETTLES_LONGEST = 5.0
# How long stopping waits for a pass in flight, in seconds.
STOPS_WITHIN = 5.0


class _Keeper:
    """The one thread that asks again, and how it is woken."""

    def __init__(self) -> None:
        self._woken = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        # Derive in the caller's thread as soon as it is woken: for a test,
        # whose rows another thread's connection cannot see.
        self.inline = False
        # How long the next pass may wait for writes to settle.
        self.within = SETTLES_WITHIN

    def wake(self) -> None:
        if self.inline:
            derivations.derive_ahead()
        else:
            self._woken.set()

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="hq-derive-ahead", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._woken.set()
        if self._thread is not None:
            self._thread.join(STOPS_WITHIN)
            self._thread = None

    def _run(self) -> None:
        while True:
            self._woken.wait(self._pause())
            if self._stopping.is_set():
                return
            self._settle()
            # Outside Django's request cycle, so the connection is checked
            # before the pass and released after it, as the request signals would.
            close_old_connections()
            try:
                derivations.derive_ahead()
            except Exception:  # the next pass tries again
                logger.exception("ahead.pass_failed")
            finally:
                close_old_connections()
            self.interrupted(self._woken.is_set())

    def interrupted(self, written: bool) -> None:
        """A pass ended, with or without writes landing while it derived."""

        self.within = min(self.within * 2, SETTLES_LONGEST) if written else SETTLES_WITHIN

    def _settle(self) -> None:
        """Wait until no write has landed for ``SETTLES``, or for ``within``."""

        latest = monotonic() + self.within
        while monotonic() < latest:
            self._woken.clear()
            if not self._woken.wait(SETTLES) or self._stopping.is_set():
                return
        self._woken.clear()

    @staticmethod
    def _pause() -> float | None:
        """Seconds until the earliest remembered answer stops standing."""

        due = derivations.next_due()
        if due is None:
            return None
        return max(RESTS, (due - timezone.now()).total_seconds())


KEEPER = _Keeper()


def note_write(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]) -> Any:
    """Run the statement; if it wrote a table a remembered question reads, say
    so when its transaction commits."""

    result = execute(sql, params, many, context)
    found = _WRITTEN.match(sql)
    if found is not None and derivations.asked_of(found[1]):
        connection = context["connection"]
        try:
            transaction.on_commit(KEEPER.wake, using=connection.alias)
        except transaction.TransactionManagementError:
            # Autocommit is off and no atomic block is open: the commit is the
            # caller's to make, and the pass reads only what has committed.
            KEEPER.wake()
    return result


def _watch(connection: Any, **kwargs: Any) -> None:
    # First in the list, not last: ``connection.execute_wrapper()`` removes the
    # last one when its block ends, and a connection can open inside one.
    if note_write not in connection.execute_wrappers:
        connection.execute_wrappers.insert(0, note_write)


def _unwatch() -> None:
    connection_created.disconnect(dispatch_uid="hq.ahead")
    for connection in connections.all(initialized_only=True):
        if note_write in connection.execute_wrappers:
            connection.execute_wrappers.remove(note_write)


@contextmanager
def keeping(*, inline: bool = False) -> Iterator[None]:
    """Keep derived answers current until the block ends.

    Every connection opened inside it is watched for writes. ``inline`` derives
    in the writer's own thread once its transaction commits, and starts no
    thread.
    """

    connection_created.connect(_watch, dispatch_uid="hq.ahead", weak=False)
    for connection in connections.all(initialized_only=True):
        _watch(connection)
    KEEPER.inline = inline
    if not inline:
        KEEPER.start()
    try:
        yield
    finally:
        if not inline:
            KEEPER.stop()
        KEEPER.inline = False
        _unwatch()
