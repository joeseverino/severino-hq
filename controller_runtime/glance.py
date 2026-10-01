"""The dashboard glance: host, container, weather and infrastructure readings on demand."""

from __future__ import annotations

import json
import logging
from typing import Any

from application.labels import human_bytes as _human_bytes
from application.timestamps import moment
from application.ui import MISSING
from control_plane.providers import controller_id
from control_plane.provider_adapters.contracts import ProviderError
from . import commands, connection_env, portainer, provider_http


logger = logging.getLogger("severino.controller")


def _container_cpu_percent(stats: dict[str, Any]) -> float:
    cpu = stats.get("cpu_stats") or {}
    previous = stats.get("precpu_stats") or {}
    cpu_delta = float((cpu.get("cpu_usage") or {}).get("total_usage") or 0) - float(
        (previous.get("cpu_usage") or {}).get("total_usage") or 0
    )
    system_delta = float(cpu.get("system_cpu_usage") or 0) - float(
        previous.get("system_cpu_usage") or 0
    )
    online = float(
        cpu.get("online_cpus")
        or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or ())
        or 1
    )
    return (
        (cpu_delta / system_delta) * online * 100
        if cpu_delta > 0 and system_delta > 0
        else 0.0
    )


_HOST_GLANCE_SCRIPT = b"""\
import json
import os
import shutil
import time

def cpu_reading():
    with open('/proc/stat', encoding='utf-8') as source:
        values = [int(value) for value in source.readline().split()[1:]]
    return sum(values), values[3] + values[4]

before_total, before_idle = cpu_reading()
time.sleep(0.2)
after_total, after_idle = cpu_reading()
elapsed = after_total - before_total
cpu = 100 * (1 - ((after_idle - before_idle) / elapsed)) if elapsed else 0
memory = {}
with open('/proc/meminfo', encoding='utf-8') as source:
    for line in source:
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
total_memory = memory.get('MemTotal', 0)
available_memory = memory.get('MemAvailable', memory.get('MemFree', 0))
disk = shutil.disk_usage('/')
print(json.dumps({
    'cpu_percent': cpu,
    'cores': os.cpu_count() or 0,
    'load_1m': os.getloadavg()[0],
    'memory_used': max(0, total_memory - available_memory),
    'memory_total': total_memory,
    'storage_used': disk.used,
    'storage_total': disk.total,
}))
"""


def _host_glance(key: str, connection_ref: str) -> dict[str, Any]:
    reading = json.loads(commands.run_ssh(connection_ref, "python3 -", _HOST_GLANCE_SCRIPT))
    memory_used = int(reading.get("memory_used") or 0)
    memory_total = int(reading.get("memory_total") or 0)
    storage_used = int(reading.get("storage_used") or 0)
    storage_total = int(reading.get("storage_total") or 0)
    memory_percent = memory_used / memory_total * 100 if memory_total else 0
    storage_percent = storage_used / storage_total * 100 if storage_total else 0
    return {
        "key": key,
        "status": "good",
        "summary": f"Host load {float(reading.get('load_1m') or 0):.2f}",
        "metrics": [
            {
                "label": "CPU",
                "value": f"{float(reading.get('cpu_percent') or 0):.0f}%",
                "detail": f"{int(reading.get('cores') or 0)} cores",
            },
            {
                "label": "Memory",
                "value": f"{memory_percent:.0f}%",
                "detail": (
                    f"{_human_bytes(memory_used)} of {_human_bytes(memory_total)} used"
                ),
            },
            {
                "label": "Storage",
                "value": f"{storage_percent:.0f}%",
                "detail": (
                    f"{_human_bytes(storage_used)} of "
                    f"{_human_bytes(storage_total)} used on /"
                ),
            },
        ],
    }


def _portainer_glance() -> dict[str, Any]:
    refs = connection_env.provider_connection_refs("portainer")
    if not refs:
        raise ProviderError("No Portainer connection was supplied.")
    machines = []
    for connection_ref in refs:
        base = portainer.portainer_url(connection_ref)
        headers = portainer.portainer_headers(connection_ref)
        for environment in portainer.load_portainer_environments(connection_ref):
            if not environment["reachable"]:
                continue
            cpu_percent = 0.0
            memory_used = 0
            storage_used = 0
            running = 0
            prefix = f"{base}/endpoints/{environment['id']}/docker"
            info = provider_http.request_json(f"{prefix}/info", headers=headers) or {}
            disk = provider_http.request_json(f"{prefix}/system/df", headers=headers) or {}
            containers = (
                provider_http.request_json(f"{prefix}/containers/json?all=false", headers=headers) or []
            )
            cores = int(info.get("NCPU") or 0)
            memory_total = int(info.get("MemTotal") or 0)
            storage_used += int(disk.get("LayersSize") or 0)
            storage_used += sum(
                int((volume.get("UsageData") or {}).get("Size") or 0)
                for volume in disk.get("Volumes") or []
            )
            storage_used += sum(
                int(item.get("Size") or 0) for item in disk.get("BuildCache") or []
            )
            for container in containers:
                stats = (
                    provider_http.request_json(
                        f"{prefix}/containers/{container['Id']}/stats?stream=false&one-shot=true",
                        headers=headers,
                    )
                    or {}
                )
                memory = stats.get("memory_stats") or {}
                cache = (memory.get("stats") or {}).get("inactive_file") or 0
                memory_used += max(0, int(memory.get("usage") or 0) - int(cache))
                cpu_percent += _container_cpu_percent(stats)
                running += 1
            machine_key = (
                controller_id()
                if environment["local"] and controller_id()
                else str(environment["name"])
            )
            machines.append(
                {
                    "key": machine_key,
                    "status": "good",
                    "summary": f"{cores} cores · {_human_bytes(memory_total)} memory",
                    "metrics": _container_metrics(
                        running, cpu_percent, cores, memory_used, memory_total, storage_used
                    ),
                }
            )
    return {
        "panel_id": "infrastructure",
        "machines": machines,
    }


def _container_metrics(
    running: int,
    cpu_percent: float,
    cores: int,
    memory_used: int,
    memory_total: int,
    storage_used: int,
) -> list[dict[str, str]]:
    """What the containers take of their machine.

    Docker states a container's CPU as a share of one core, so the sum across
    containers is divided by the cores to say how much of the machine they use;
    memory is a share of the machine's memory, with the amounts beside it.
    """

    machine_cpu = cpu_percent / cores if cores else cpu_percent
    memory_share = memory_used / memory_total * 100 if memory_total else 0
    return [
        {"label": "Containers", "value": str(running), "detail": "running"},
        {"label": "CPU", "value": f"{machine_cpu:.0f}%", "detail": f"of {cores} cores"},
        {
            "label": "Memory",
            "value": f"{memory_share:.0f}%",
            "detail": f"{_human_bytes(memory_used)} of {_human_bytes(memory_total)}",
        },
        {
            "label": "Docker storage",
            "value": _human_bytes(storage_used),
            "detail": "layers, volumes and build cache",
        },
    ]


# Hours of the forecast the dashboard looks ahead, and the chance of rain
# worth naming.
_LOOKAHEAD_HOURS = 12
_RAIN_CHANCE = 30


def _hour(period: dict[str, Any]) -> str:
    """``4 PM`` for a period's start, in the point's own time."""

    start = moment(period.get("startTime"), naive="keep")
    if start is None:
        return ""
    return f"{start.hour % 12 or 12} {'AM' if start.hour < 12 else 'PM'}"


def _chance(period: dict[str, Any]) -> int:
    value = (period.get("probabilityOfPrecipitation") or {}).get("value")
    return int(value) if isinstance(value, (int, float)) else 0


def _ahead(periods: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The next hours, as the dashboard lists them."""

    return [
        {
            "time": _hour(period),
            "temperature": f"{period.get('temperature', MISSING)}°",
            "forecast": str(period.get("shortForecast") or ""),
            "precipitation": f"{_chance(period)}%",
        }
        for period in periods
    ]


def _outlook(periods: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The range ahead and when rain next comes, from the hourly periods."""

    temperatures = [
        period["temperature"]
        for period in periods
        if isinstance(period.get("temperature"), (int, float))
    ]
    found = []
    if temperatures:
        found.append(
            {
                "label": "Range",
                "value": f"{min(temperatures)}–{max(temperatures)}°",
                "detail": f"next {len(periods)} hours",
            }
        )
    rain = next((period for period in periods if _chance(period) >= _RAIN_CHANCE), None)
    if rain is not None:
        found.append(
            {
                "label": "Rain",
                "value": f"{'Now' if rain is periods[0] else _hour(rain)} · {_chance(rain)}%",
                "detail": str(rain.get("shortForecast") or ""),
            }
        )
    return found


def _alert(features: list[Any]) -> list[dict[str, str]]:
    """The active alert by name, with how many more there are."""

    if not features:
        return []
    first = (features[0] or {}).get("properties") or {}
    more = f" +{len(features) - 1}" if len(features) > 1 else ""
    return [
        {
            "label": "Alerts",
            "value": f"{first.get('event') or 'Weather alert'}{more}",
            "detail": str(first.get("headline") or ""),
        }
    ]


def _nws_glance(point: str) -> dict[str, Any]:
    point = point.strip()
    parts = point.split(",")
    if len(parts) != 2:
        raise ProviderError("SEVERINO_NWS_POINT must be latitude,longitude.")
    try:
        latitude, longitude = (float(part.strip()) for part in parts)
    except ValueError as exc:
        raise ProviderError("SEVERINO_NWS_POINT is not numeric.") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ProviderError("SEVERINO_NWS_POINT is outside valid coordinates.")
    headers = {
        "Accept": "application/geo+json",
        "User-Agent": "Severino-HQ/1.0 (https://github.com/joeseverino/severino-hq)",
    }
    point_data = (
        provider_http.request_json(
            f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}",
            headers=headers,
        )
        or {}
    )
    properties = point_data.get("properties") or {}
    hourly = (
        provider_http.request_json(str(properties.get("forecastHourly") or ""), headers=headers) or {}
    )
    periods = [
        period
        for period in (hourly.get("properties") or {}).get("periods") or []
        if isinstance(period, dict)
    ][:_LOOKAHEAD_HOURS]
    current = periods[0] if periods else {}
    alerts = (
        provider_http.request_json(
            f"https://api.weather.gov/alerts/active?point={latitude:.4f},{longitude:.4f}",
            headers=headers,
        )
        or {}
    )
    active = alerts.get("features") or []
    place = (properties.get("relativeLocation") or {}).get("properties") or {}
    location = ", ".join(
        part for part in (place.get("city"), place.get("state")) if part
    )
    status = "serious" if active else "good"
    metrics = [
        {
            "label": "Now",
            "value": str(current.get("shortForecast") or "Unavailable"),
            "detail": str(current.get("name") or ""),
        },
        {
            "label": "Temperature",
            "value": f"{current.get('temperature', MISSING)}°{current.get('temperatureUnit', 'F')}",
            "detail": str(current.get("windChill") or ""),
        },
        *_outlook(periods),
        {
            "label": "Wind",
            "value": f"{current.get('windDirection', '')} {current.get('windSpeed', MISSING)}".strip(),
            "detail": "NWS hourly forecast",
        },
        *_alert(active),
    ]
    return {
        "panel_id": "weather",
        "point": f"{latitude:.4f},{longitude:.4f}",
        "status": status,
        "summary": location or "National Weather Service",
        "metrics": metrics,
        "hours": _ahead(periods),
    }


def _infrastructure_glance(targets: list[dict[str, Any]]) -> dict[str, Any]:
    available_ssh = set(connection_env.ssh_connection_refs())
    unresolved = [
        target
        for target in targets
        if not any(str(ref) in available_ssh for ref in target.get("connections") or [])
    ]
    docker_by_key = {}
    if unresolved:
        portainer = _portainer_glance()
        docker_by_key = {
            str(item.get("key", "")): item for item in portainer.get("machines", [])
        }
    machines = []
    for target in targets:
        key = str(target.get("key", "")).strip()
        connection_ref = next(
            (
                str(ref)
                for ref in target.get("connections") or []
                if str(ref) in available_ssh
            ),
            "",
        )
        if connection_ref:
            machines.append(_host_glance(key, connection_ref))
        elif key in docker_by_key:
            machines.append(docker_by_key[key])
        else:
            machines.append(
                {
                    "key": key,
                    "status": "attention",
                    "summary": "No host telemetry connection is available.",
                    "metrics": [],
                }
            )
    return {"panel_id": "infrastructure", "machines": machines}


def dashboard_glance(plan: dict[str, Any]) -> list[dict[str, Any]]:
    readings = []
    panel_ids = plan.get("panels") if isinstance(plan, dict) else []
    targets = plan.get("targets") if isinstance(plan, dict) else {}
    for panel_id in panel_ids or []:
        try:
            if panel_id == "infrastructure":
                readings.append(_infrastructure_glance(targets.get(panel_id) or []))
            elif panel_id == "weather":
                readings.append(
                    _nws_glance(str((targets.get("weather") or {}).get("point", "")))
                )
        except (ProviderError, OSError, ValueError, KeyError) as exc:
            # Logged here; HQ keeps the last good reading and shows this as a note.
            provider_http.logger.warning(
                "dashboard glance failed: %s (%s): %s",
                panel_id,
                type(exc).__name__,
                str(exc)[:200],
                extra={"event": "controller.glance.failed", "panel": panel_id},
            )
            # Marked, so HQ keeps the last good reading instead of this one.
            failure = {
                "status": "serious",
                "summary": f"Refresh failed ({type(exc).__name__}).",
                "metrics": [],
                "refresh_failed": type(exc).__name__,
            }
            readings.append(
                {
                    "panel_id": panel_id,
                    "machines": [{"key": controller_id(), **failure}],
                }
                if panel_id == "infrastructure"
                else {
                    "panel_id": panel_id,
                    "point": str((targets.get("weather") or {}).get("point", "")),
                    **failure,
                }
            )
    return readings
