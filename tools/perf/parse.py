"""What the measuring tools print, read into numbers."""

import re
import statistics

# One metric of a Server-Timing header: its name, then `;dur=<ms>` among its parameters.
_METRIC = re.compile(r"(?:^|,)\s*([!#$%&'*+.^_`|~\w-]+)((?:\s*;[^,]*)*)")
_DURATION = re.compile(r";\s*dur\s*=\s*([0-9.]+)")
# `footprint -f bytes` prints each process's resident footprint as this line.
_FOOTPRINT = re.compile(r"^\s*phys_footprint:\s*(\d+)\s*B\s*$", re.MULTILINE)
# How HQ refuses an extension built for another plugin API
# (hq/platform/application/plugins.py), as a server's log holds it.
_API_REFUSAL = re.compile(r"Plugin \S+ requires API \d+; HQ supports \d+\.?")
# `docker stats --format '{{.MemUsage}}'`: the usage, a slash, the limit.
_DOCKER_MEMORY = re.compile(r"^\s*([0-9.]+)\s*([kKMGT]?i?B)\s*/")
_UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def server_time(header: str, metric: str = "app") -> float | None:
    """The duration, in milliseconds, a Server-Timing header gives ``metric``; None without one."""

    for match in _METRIC.finditer(header or ""):
        if match.group(1) == metric and (duration := _DURATION.search(match.group(2))):
            return float(duration.group(1))
    return None


def percentiles(values: list[float]) -> tuple[float, float]:
    """The median and the 95th percentile, by nearest rank, of at least one value."""

    ordered = sorted(values)
    p95 = ordered[max(0, round(len(ordered) * 0.95) - 1)]
    return round(statistics.median(ordered), 2), round(p95, 2)


def footprint_bytes(output: str) -> int:
    """The footprint of every process `footprint -f bytes` reported, together."""

    found = [int(value) for value in _FOOTPRINT.findall(output)]
    if not found:
        raise ValueError(f"footprint reported no process: {output.strip()[:200]!r}")
    return sum(found)


def docker_memory_bytes(output: str) -> int:
    """A container's memory in use, from `docker stats --no-stream --format '{{.MemUsage}}'`."""

    match = _DOCKER_MEMORY.match(output)
    if not match or match.group(2) not in _UNITS:
        raise ValueError(f"docker stats said no memory in use: {output.strip()!r}")
    return round(float(match.group(1)) * _UNITS[match.group(2)])


def api_refusal(log: str) -> str | None:
    """HQ's refusal of an extension built for another plugin API, from a server's log; None without one."""

    found = _API_REFUSAL.search(log)
    return found.group(0) if found else None


def cookie(output: str) -> tuple[str, str]:
    """(name, value) from the `<name>=<value>` line `manage.py dev_session` ends on."""

    lines = output.strip().splitlines()
    name, _, value = lines[-1].partition("=") if lines else ("", "", "")
    if not name or not value or any(char.isspace() for char in name + value):
        raise ValueError(f"dev_session printed no cookie: {output.strip()[-200:]!r}")
    return name, value
