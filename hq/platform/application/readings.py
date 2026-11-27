"""Values HQ keeps of its own, each with when it was true: when a worker or a
channel last arrived, and what HQ last noted of itself.

A reading of a service outside HQ is an observation kind's, in the inventory
(``inventory.keep``), never a value here.
"""

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from hq.platform.core.models import UpstreamReading

from .projection import read_once

# Every heartbeat's key ends so, and when each was noted is held the same way.
_HEARTBEAT = "-heartbeat"
_HEARD = "readings.heard"


def stored(key: str) -> UpstreamReading | None:
    return UpstreamReading.objects.filter(key=key).first()


def record(key: str, value: Any, *, observed_at: datetime | None = None) -> None:
    """Store a value. ``observed_at`` is when it was true, if not now."""
    UpstreamReading.objects.update_or_create(
        key=key, defaults={"value": value, "observed_at": observed_at or timezone.now()}
    )
    read_once(_HEARD, list).clear()


def heartbeat(name: str) -> str:
    """The reading that holds when ``name`` last arrived."""

    return f"{name}{_HEARTBEAT}"


def note(name: str, *, every: timedelta) -> bool:
    """Record that ``name`` arrived now, at most once per ``every``: a
    heartbeat holds a moment and nothing else, and an age is worded no finer.
    Whether it was recorded."""

    reading = stored(heartbeat(name))
    if reading is not None and timezone.now() - reading.observed_at < every:
        return False
    record(heartbeat(name), {})
    return True


def heard(name: str) -> datetime | None:
    """When ``name`` last arrived, read with every other heartbeat in one
    query a projection, so each worker or channel that reports adds none."""

    found = read_once(_HEARD, list)
    if not found:
        found.append(dict(UpstreamReading.objects.filter(key__endswith=_HEARTBEAT).values_list("key", "observed_at")))
    return found[0].get(heartbeat(name))
