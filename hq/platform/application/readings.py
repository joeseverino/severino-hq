"""The last value HQ read from a service outside it, and when.

Pages read the stored value, so no page waits on the network for one. Reading
again happens where waiting costs nobody: a request the browser makes after the
page, or the write that changed the value.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from django.utils import timezone

from hq.platform.core.models import UpstreamReading

from .projection import read_once

_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
# Each key asked within one projection, with its reading or None for none.
_ASKED = "readings.asked"


def stored(key: str) -> UpstreamReading | None:
    return UpstreamReading.objects.filter(key=key).first()


def stored_many(keys) -> dict[str, UpstreamReading]:
    """The stored readings for ``keys`` that exist, in one query for the keys
    the projection has not asked yet."""

    keys = list(keys)
    asked = read_once(_ASKED, dict)
    if missing := [key for key in keys if key not in asked]:
        found = {reading.key: reading for reading in UpstreamReading.objects.filter(key__in=missing)}
        asked.update((key, found.get(key)) for key in missing)
    return {key: reading for key in keys if (reading := asked[key]) is not None}


def refresh(key: str, fetch: Callable[[], Any], *, older_than: timedelta) -> None:
    """Read again if the stored value is missing or older than ``older_than``.

    ``fetch`` returns the value to store, or raises to leave the last one as is.
    """

    reading = stored(key)
    if reading and timezone.now() - reading.observed_at < older_than:
        return
    record(key, fetch())


def record(key: str, value: Any, *, observed_at: datetime | None = None) -> None:
    """Store a value. ``observed_at`` is when it was true, if not now."""
    UpstreamReading.objects.update_or_create(
        key=key, defaults={"value": value, "observed_at": observed_at or timezone.now()}
    )
    read_once(_ASKED, dict).pop(key, None)


def machine_telemetry(machine_key: str) -> str:
    """The reading key for one machine's CPU, memory and container figures.

    A reading, not resource state: stored on the resource it made every refresh
    a status change, and every status change an audit row.
    """
    return f"machine-telemetry:{machine_key}"


def expire(key: str) -> None:
    """Keep the value, but have the next refresh read again."""

    UpstreamReading.objects.filter(key=key).update(observed_at=_LONG_AGO)
    read_once(_ASKED, dict).pop(key, None)
