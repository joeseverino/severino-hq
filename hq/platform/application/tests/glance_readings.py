"""Dashboard readings as the controller reports them (glance_shapes), for the
tests that store one."""

from typing import Any

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

# A machine read through Docker: three containers using 37% of its CPU.
DOCKER = {
    "containers": 3,
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
