"""Derived answers kept current for as long as the web application runs, and
the pages open on HQ told once they are.

An answer stops standing when a table it reads is written or its moment comes,
and with a controller reporting every few minutes the next request would pay to
derive it again. So one thread asks again once the write commits
(docs/Architecture.md, Asked again when the change lands). Nothing here decides
what is true: a request that comes first derives for itself.

The thread learns that anything committed, from any connection in any
process, through SQLite's ``PRAGMA data_version`` on a connection of its own
(sqlite.org/pragma.html#pragma_data_version), and which tables were written
from their revisions (``core.revisions``), which the database moves in the
writing transaction. A rolled-back write moves neither.
"""

import logging
import math
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from time import monotonic

from django.db import connection
from django.utils import timezone

from hq.platform.application import derivations
from hq.platform.core import doorbell, live, revisions
from hq.platform.core.outside_requests import connections_of_its_own

logger = logging.getLogger("severino.ahead")

# How often the thread looks for a commit, in seconds.
LOOKS_EVERY = 0.25
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


def _version() -> int:
    """What this thread's connection last saw committed by any other."""

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA data_version")
        (version,) = cursor.fetchone()
    return int(version)


class Keeper:
    """The one thread that asks again, and what it last saw."""

    def __init__(self) -> None:
        self._stopping = threading.Event()
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        # The revisions as the last pass read them; None before the first.
        self._seen: Mapping[str, int] | None = None
        # How long the next pass may wait for writes to settle.
        self.within = SETTLES_WITHIN

    def start(self) -> None:
        """Start the thread, once it has read what it measures later writes from."""

        self._stopping.clear()
        self._started.clear()
        self._thread = threading.Thread(target=self._run, name="hq-derive-ahead", daemon=True)
        self._thread.start()
        self._started.wait(STOPS_WITHIN)

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(STOPS_WITHIN)
            self._thread = None

    def turn(self, *, due: bool = False) -> set[str]:
        """One pass: the tables written since the last, every question that
        reads one asked again (every one, when an answer's moment is ``due``),
        and only then the pages and the doorbell told. The tables written."""

        found = revisions.read()
        if found is None:
            return set()
        was, self._seen = self._seen, dict(found.counts)
        changed = (
            {table for table in was.keys() | self._seen.keys() if was.get(table) != self._seen.get(table)}
            if was is not None
            else set()
        )
        try:
            if due or any(map(derivations.asked_of, changed)):
                derivations.derive_ahead()
        finally:
            live.HUB.told(changed)
            doorbell.heard(changed)
        return changed

    def _run(self) -> None:
        version: int | None = None
        due = -math.inf
        with connections_of_its_own():
            while True:
                try:
                    now = _version()
                    if now != version or monotonic() >= due:
                        # The first pass reads what every later one is measured from.
                        if version is not None and now != version:
                            now = self._settle(now)
                        self.turn(due=monotonic() >= due)
                        self.interrupted(written=_version() != now)
                        version, due = now, self._due()
                except Exception:  # the next pass tries again
                    logger.exception("ahead.pass_failed")
                self._started.set()
                if self._stopping.wait(LOOKS_EVERY):
                    return

    def interrupted(self, *, written: bool) -> None:
        """A pass ended, with or without writes landing while it derived.

        While writes keep landing (an import) what a pass derives is unsettled
        before it is read, so each such pass doubles the wait for them to settle,
        up to ``SETTLES_LONGEST``; the first pass nothing interrupts restores it.
        """

        self.within = min(self.within * 2, SETTLES_LONGEST) if written else SETTLES_WITHIN

    def _settle(self, version: int) -> int:
        """Wait until no write has landed for ``SETTLES``, or for ``within``; what it settled at."""

        latest = monotonic() + self.within
        while monotonic() < latest and not self._stopping.wait(SETTLES):
            if (now := _version()) == version:
                break
            version = now
        return version

    def _due(self) -> float:
        """When, on the monotonic clock, the earliest remembered answer stops standing."""

        pause = self._pause()
        return math.inf if pause is None else monotonic() + pause

    @staticmethod
    def _pause() -> float | None:
        """Seconds until the earliest remembered answer stops standing."""

        due = derivations.next_due()
        if due is None:
            return None
        return max(RESTS, (due - timezone.now()).total_seconds())


KEEPER = Keeper()


@contextmanager
def keeping() -> Iterator[None]:
    """Keep derived answers current, and tell the pages open on HQ what
    changed, until the block ends."""

    KEEPER.start()
    try:
        yield
    finally:
        KEEPER.stop()
