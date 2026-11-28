"""Ring the controller's doorbell when a pass is due.

The host runs a pass when HQ rings, and otherwise only at its floor
(`cadence.CONTROLLER_FLOOR`). One thread asks whether a pass is due when a
write that can make one due commits, from any writer (the keeper of derived
answers hears it, ``heard``), at once when a capability's write
commits (`ask_soon`), and otherwise sleeps until something comes due with time
alone (`cadence.next_due`), so no request pays for the asking and an idle HQ
asks nothing.
"""

import logging
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from django.utils import timezone

from hq.platform.application import cadence
from hq.platform.core.outside_requests import connections_of_its_own

logger = logging.getLogger("severino.doorbell")

# How long a pass that is due, and rung for, waits to be asked again when
# nothing it reads is written, in seconds: the controller answering writes.
AGAIN = 60.0
# How long past a moment the thread wakes, in seconds, so the moment has come.
LATE_BY = 1.0
# How long stopping waits for an ask in flight, in seconds.
STOPS_WITHIN = 5.0

# Set to have the running thread ask now; writes that land together are one ask.
_woken = threading.Event()
# Set while the thread runs, the only time a wake has anyone to hear it.
_listening = threading.Event()


def ask() -> bool:
    """Ring if a pass is due. Returns whether it rang."""

    return cadence.ring_when_due()


def wake() -> None:
    """Have the doorbell's thread ask now, if it runs."""

    if _listening.is_set():
        _woken.set()


def ask_soon() -> None:
    """Have the doorbell's thread ask now, off the caller's thread.

    Where no thread runs (a management command, a test) the caller asks
    itself, so a write made there still starts its pass at once.
    """

    if _listening.is_set():
        wake()
    else:
        ask()


def heard(tables: Iterable[str]) -> None:
    """The keeper found ``tables`` written: ask again if one can make a pass due."""

    if not cadence.makes_due().isdisjoint(tables):
        wake()


def _pause(rang: bool) -> float | None:
    """Seconds until the next ask with nothing written; None for none."""

    due = None if rang else cadence.next_due()
    if due is None:
        return AGAIN if rang else None
    left = (due - timezone.now()).total_seconds()
    # A moment already come that rang nothing (an unwritable doorbell) is
    # asked again as a rung one is.
    return left + LATE_BY if left > 0 else AGAIN


@contextmanager
def ringing_when_due() -> Iterator[None]:
    """Ask as the thread is woken or something comes due, for as long as the web application runs."""

    stopping = threading.Event()

    def run() -> None:
        pause: float | None = 0.0
        while True:
            _woken.wait(pause)
            if stopping.is_set():
                return
            _woken.clear()
            with connections_of_its_own():
                try:
                    pause = _pause(bool(ask()))
                except Exception:
                    logger.exception("doorbell.ask_failed")
                    pause = AGAIN

    _woken.clear()
    thread = threading.Thread(target=run, name="hq-doorbell", daemon=True)
    thread.start()
    _listening.set()
    try:
        yield
    finally:
        _listening.clear()
        stopping.set()
        _woken.set()
        thread.join(STOPS_WITHIN)
