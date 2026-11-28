"""What the measuring tools print, read into numbers."""

import re
import statistics

# One metric of a Server-Timing header: its name, then `;dur=<ms>` among its parameters.
_METRIC = re.compile(r"(?:^|,)\s*([!#$%&'*+.^_`|~\w-]+)((?:\s*;[^,]*)*)")
_DURATION = re.compile(r";\s*dur\s*=\s*([0-9.]+)")
# What each system reports a process's memory as, as tools/lib/measured reads it, and the bytes in
# its unit: `footprint -f bytes` on macOS prints each process's footprint as the first line, and
# Linux's smaps_rollup gives a process's proportional set size and its swapped share as the second.
_MEMORY = (
    (re.compile(r"^\s*phys_footprint:\s*(\d+)\s*B\s*$", re.MULTILINE), 1),
    (re.compile(r"^(?:Swap)?Pss:\s+(\d+) kB$", re.MULTILINE), 1024),
)


def server_time(header: str, metric: str = "app") -> float | None:
    """The duration, in milliseconds, a Server-Timing header gives ``metric``; None without one."""

    for match in _METRIC.finditer(header or ""):
        if match.group(1) == metric and (duration := _DURATION.search(match.group(2))):
            return float(duration.group(1))
    return None


def percentiles(values: list[float]) -> tuple[float, float]:
    """The median and the 95th percentile of at least one value, each interpolated between the
    two values either side of it."""

    cuts = statistics.quantiles(values, n=20, method="inclusive")
    return round(cuts[9], 2), round(cuts[18], 2)


def memory_bytes(output: str) -> int:
    """The memory of every process ``output`` reports, together: what `footprint -f bytes` printed,
    or the /proc smaps_rollup of each process."""

    found = [int(value) * unit for pattern, unit in _MEMORY for value in pattern.findall(output)]
    if not found:
        raise ValueError(f"no process's memory was reported: {output.strip()[:200]!r}")
    return sum(found)


def container_anon_bytes(memory_stat: str) -> int:
    """The anonymous memory a container's processes hold, from its cgroup's memory.stat."""

    stat = dict(pair for pair in (line.split() for line in memory_stat.splitlines()) if len(pair) == 2)
    anon = stat.get("anon", "")
    if not anon.isdigit():
        raise ValueError(f"memory.stat names no anonymous memory: {memory_stat.strip()[:80]!r}")
    return int(anon)


def cookie(output: str) -> tuple[str, str]:
    """(name, value) from the `<name>=<value>` line `manage.py dev_session` ends on."""

    lines = output.strip().splitlines()
    name, _, value = lines[-1].partition("=") if lines else ("", "", "")
    if not name or not value or any(char.isspace() for char in name + value):
        raise ValueError(f"dev_session printed no cookie: {output.strip()[-200:]!r}")
    return name, value
