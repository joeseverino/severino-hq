"""Dashboard readings as the controller reports them (glance_shapes), for the
tests that store one, and the store they are kept in."""

from datetime import datetime
from typing import Any

from django.utils import timezone

from hq.domains.control_plane.observations.glance import MACHINE_KIND, WEATHER_KIND

from ..facts import kept_by
from ..inventory import keep

# A machine read over SSH: 9% of its CPU, a quarter of its memory and half its
# disk in use.
HOST = {
    "cpu_percent": 9.2,
    "cores": 8,
    "load_1m": 0.3,
    "memory_used": 4_294_967_296,
    "memory_total": 17_179_869_184,
    "storage_used": 53_687_091_200,
    "storage_total": 107_374_182_400,
}

# A machine read through Docker: its containers using 37% of its CPU.
DOCKER = {
    "cpu_percent": 37.0,
    "cores": 4,
    "memory_used": 2_147_483_648,
    "memory_total": 8_589_934_592,
    "storage": 3_221_225_472,
}


def reported_machine(key: str, **parts: Any) -> dict[str, Any]:
    """One machine's reading: ``host=``, ``docker=`` or ``refresh_failed=``."""

    return {"key": key, **parts}


def reported_hour(start: str, temperature: float | None = 70, **fields: Any) -> dict[str, Any]:
    """One hour of the forecast, starting at ``start`` (an ISO 8601 instant)."""

    found = {"start": start, "temperature_unit": "F", **fields}
    if temperature is not None:
        found["temperature"] = temperature
    return found


def reported_weather(point: str, *hours: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """The forecast at ``point``."""

    return {"point": point, "hours": list(hours), **fields}


def keep_machine(reading: dict[str, Any], *, at: datetime | None = None) -> None:
    """Store a machine's reading, read ``at`` (now by default)."""

    keep(MACHINE_KIND, "key", reading, read_at=at or timezone.now())


def keep_weather(reading: dict[str, Any], *, at: datetime | None = None) -> None:
    """Store the forecast at a point, read ``at`` (now by default)."""

    keep(WEATHER_KIND, "point", reading, read_at=at or timezone.now())


def kept_machine(key: str) -> Any:
    """The stored reading of one machine, or None."""

    return kept_by(MACHINE_KIND, "key").get(key)


def kept_weather(point: str) -> Any:
    """The stored forecast at ``point``, or None."""

    return kept_by(WEATHER_KIND, "point").get(point)
