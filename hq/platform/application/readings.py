"""The last value HQ read from a service outside it, and when.

Pages read the stored value, so no page waits on the network for one. Reading
again happens where waiting costs nobody: a request the browser makes after the
page, or the write that changed the value.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import reduce
from operator import or_
from typing import Any

from django.db.models import Q
from django.utils import timezone

from hq.domains.control_plane.observations.public_registry import RDAP
from hq.platform.core.models import UpstreamReading

from .freshness import DASHBOARD_GLANCE, PAGE_VISIT
from .projection import read_once

# Every reading of a kept kind by its key, held once a projection asked for one.
_KEPT = "readings.kept"
# Every heartbeat's key ends so, and when each was noted is held the same way.
_HEARTBEAT = "-heartbeat"
_HEARD = "readings.heard"


def stored(key: str) -> UpstreamReading | None:
    return UpstreamReading.objects.filter(key=key).first()


def stored_many(keys: Iterable[str]) -> dict[str, UpstreamReading]:
    """The stored readings for ``keys``, each of a kept kind, that exist."""

    held = _kept()
    return {key: held[key] for key in keys if key in held}


def record(key: str, value: Any, *, observed_at: datetime | None = None) -> None:
    """Store a value. ``observed_at`` is when it was true, if not now."""
    UpstreamReading.objects.update_or_create(
        key=key, defaults={"value": value, "observed_at": observed_at or timezone.now()}
    )
    read_once(_KEPT, list).clear()
    read_once(_HEARD, list).clear()


def heartbeat(name: str) -> str:
    """The reading that holds when ``name`` last arrived."""

    return f"{name}{_HEARTBEAT}"


def note(name: str, *, every: timedelta) -> None:
    """Record that ``name`` arrived now, at most once per ``every``: a
    heartbeat holds a moment and nothing else, and an age is worded no finer."""

    reading = stored(heartbeat(name))
    if reading is None or timezone.now() - reading.observed_at >= every:
        record(heartbeat(name), {})


def heard(name: str) -> datetime | None:
    """When ``name`` last arrived, read with every other heartbeat in one
    query a projection, so each worker or channel that reports adds none."""

    found = read_once(_HEARD, list)
    if not found:
        found.append(dict(UpstreamReading.objects.filter(key__endswith=_HEARTBEAT).values_list("key", "observed_at")))
    return found[0].get(heartbeat(name))


def _kept() -> dict[str, UpstreamReading]:
    """Every reading of every kept kind, in one query a projection."""

    found = read_once(_KEPT, list)
    if not found:
        wanted = reduce(or_, (Q(key__startswith=item("")) for item in KEPT))
        found.append({reading.key: reading for reading in UpstreamReading.objects.filter(wanted)})
    return found[0]


# The keyless connections HQ reaches itself, by the kind their rows carry.
NWS = "nws"
PUBLIC_DNS = "public_dns"


@dataclass(frozen=True, slots=True)
class Kept:
    """A kind of reading kept here, and the connections HQ reaches itself
    that it comes through, as ``ObservationSpec.provider`` says of a sweep's.

    Called with its subject, it is that reading's key. ``through`` maps each
    connection kind it is read through to whether a stored value says that
    connection answered; ``cadence`` is the kind its age is judged by.
    """

    prefix: str
    label: str
    cadence: str
    through: Mapping[str, Callable[[Any], bool]]

    def __call__(self, subject: str) -> str:
        return f"{self.prefix}:{subject}"

    def stored(self) -> list[UpstreamReading]:
        """Every reading of this kind, whatever its subject."""

        return [reading for key, reading in _kept().items() if key.startswith(self(""))]


# One machine's CPU, memory and container figures. A reading, not resource
# state: stored on the resource it made every refresh a status change, and
# every status change an audit row. It comes through the controller, whose
# connections its own reports date.
machine_telemetry = Kept("machine-telemetry", "Machine figures", DASHBOARD_GLANCE, {})

# The forecast at one point, as latitude,longitude, which the controller reads
# from the National Weather Service when the dashboard opens.
weather = Kept("weather", "Weather forecast", DASHBOARD_GLANCE, {NWS: lambda value: not value.get("refresh_failed")})

# What the public registries say about one address: its reverse DNS from the
# resolver, and who holds it from RDAP. The answer barely moves, and asking
# again spends a stranger's rate limit and tells them which addresses HQ cares
# about, so the connection panel and the tools page share the one stored
# answer, read when a person asks.
public_address = Kept(
    "public-address",
    "Address lookups",
    PAGE_VISIT,
    {PUBLIC_DNS: lambda value: "arpa" in value, RDAP: lambda value: bool(value.get("allocation"))},
)

KEPT = (machine_telemetry, weather, public_address)
