"""Ring the controller's doorbell when a pass is due.

The host runs a pass when HQ rings, and otherwise only at its floor
(`cadence.CONTROLLER_FLOOR`). A request that queues work rings itself; this
asks once a minute for what comes due with time alone (a sweep, an expired
lease).
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


def ask() -> bool:
    """Ring if a pass is due. Returns whether it rang."""

    return bool(cadence.pass_due()) and cadence.ring_doorbell()


@contextmanager
def ringing_when_due() -> Iterator[None]:
    """Ask once a minute for as long as the web application runs."""

    stopping = threading.Event()

    def run() -> None:
        while not stopping.wait(ASKS_EVERY):
            # Not a request, so do what Django's request signals do: drop stale connections.
            close_old_connections()
            try:
                ask()
            except Exception:  # the next minute asks again
                logger.exception("doorbell.ask_failed")
            finally:
                close_old_connections()

    thread = threading.Thread(target=run, name="hq-doorbell", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        thread.join(STOPS_WITHIN)
