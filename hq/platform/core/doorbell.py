"""Ring the controller's doorbell when a pass is due.

The host runs a pass when HQ rings, and otherwise only at its floor
(`cadence.CONTROLLER_FLOOR`). One thread asks once a minute for what comes due
with time alone (a sweep, an expired lease) or was written outside a
capability, and at once when a capability's write commits (`ask_soon`), so no
request pays for the asking.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import close_old_connections

from hq.platform.application import cadence

logger = logging.getLogger("severino.doorbell")

# Seconds between asks: the most that time-based work waits to start.
ASKS_EVERY = 60.0
# How long stopping waits for an ask in flight, in seconds.
STOPS_WITHIN = 5.0

# Set to have the running thread ask now; writes that land together are one ask.
_woken = threading.Event()
# Set while the thread runs, the only time a wake has anyone to hear it.
_listening = threading.Event()


def ask() -> bool:
    """Ring if a pass is due. Returns whether it rang."""

    return cadence.ring_when_due()


def ask_soon() -> None:
    """Have the doorbell's thread ask now, off the caller's thread.

    Where no thread runs (a management command, a test) the caller asks
    itself, so a write made there still starts its pass at once.
    """

    if _listening.is_set():
        _woken.set()
    else:
        ask()


@contextmanager
def ringing_when_due() -> Iterator[None]:
    """Ask once a minute, and whenever woken, for as long as the web application runs."""

    stopping = threading.Event()

    def run() -> None:
        while True:
            _woken.wait(ASKS_EVERY)
            if stopping.is_set():
                return
            _woken.clear()
            # Not a request, so do what Django's request signals do: drop stale connections.
            close_old_connections()
            try:
                ask()
            except Exception:  # the next minute asks again
                logger.exception("doorbell.ask_failed")
            finally:
                close_old_connections()

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
