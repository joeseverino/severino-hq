"""Derived answers kept current for as long as the web application runs.

An answer stops standing when a table it reads is written or its moment comes,
and with a controller reporting every few minutes the next request would pay to
derive it again. So one thread asks again once the write commits
(docs/Architecture.md, Asked again when the change lands). Nothing here decides
what is true: a request that comes first derives for itself.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic

from django.utils import timezone

from hq.platform.application import derivations
from hq.platform.core import live
from hq.platform.core.outside_requests import connections_of_its_own

logger = logging.getLogger("severino.ahead")

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
            with connections_of_its_own():
                try:
                    derivations.derive_ahead()
                except Exception:  # the next pass tries again
                    logger.exception("ahead.pass_failed")
            self.interrupted(written=self._woken.is_set())

    def interrupted(self, *, written: bool) -> None:
        """A pass ended, with or without writes landing while it derived.

        While writes keep landing (an import) what a pass derives is unsettled
        before it is read, so each such pass doubles the wait for them to settle,
        up to ``SETTLES_LONGEST``; the first pass nothing interrupts restores it.
        """

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


def _written(table: str) -> None:
    """A committed write: news when a remembered question reads its table."""

    if derivations.asked_of(table):
        KEEPER.wake()


@contextmanager
def keeping(*, inline: bool = False) -> Iterator[None]:
    """Keep derived answers current until the block ends.

    It hears each committed write (``live.heard``). ``inline`` derives in the
    writer's own thread once its transaction commits, and starts no thread.
    """

    KEEPER.inline = inline
    if not inline:
        KEEPER.start()
    try:
        with live.heard(_written):
            yield
    finally:
        if not inline:
            KEEPER.stop()
        KEEPER.inline = False
