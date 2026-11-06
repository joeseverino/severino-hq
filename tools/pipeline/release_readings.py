"""What the deploy host says about the release it runs, read as any account: one JSON document.

`mise run release:report` sends this file over SSH to the host's own python3
(`python3 -I -`), so it is the standard library alone, at the host's Python,
and reads only what an account with no sudo and no docker group can: the
serving container's cgroup files (world-readable), systemd's record of each
unit's last run, and the journal of the drift check's last run. A reading that
fails is an ``error`` in its place, so one missing file never hides the rest.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The argument the image's CMD hands uvicorn: the one process that is HQ serving.
SERVING = "hq.config.asgi:application"
SCOPE = re.compile(r"^0::(/system\.slice/docker-([0-9a-f]{64})\.scope)$", re.MULTILINE)
# The units whose last run the report times.
TIMED = ("severino-hq-controller.service", "severino-hq-glance.service")
DRIFT = "severino-hq-script-drift.service"
PROPERTIES = (
    "Result",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "CPUUsageNSec",
    "MemoryPeak",
    "InvocationID",
)
# Every call is bounded, so a hung systemctl is a failed reading.
TIMEOUT = 20


def ran(*argv: str) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if done.returncode:
        raise RuntimeError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def serving(proc: Path) -> list[tuple[int, str, str]]:
    """Each process serving HQ: its pid, its cgroup's path and its container's id."""

    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            cgroup = (entry / "cgroup").read_text(encoding="utf-8")
        except OSError:  # noqa: S112 - a process that exits mid-read serves nothing
            continue
        if SERVING.encode() in argv and (scope := SCOPE.search(cgroup)):
            found.append((int(entry.name), scope.group(1), scope.group(2)))
    return sorted(found)


def keyed(text: str) -> dict[str, int]:
    """A cgroup file of ``name value`` lines (cpu.stat, memory.stat)."""

    pairs = (line.split() for line in text.splitlines())
    return {pair[0]: int(pair[1]) for pair in pairs if len(pair) == 2 and pair[1].isdigit()}


def container(proc: Path, cgroups: Path) -> dict:
    found = serving(proc)
    scopes = sorted({(path, ident) for _, path, ident in found})
    if not scopes:
        raise RuntimeError(f"no process runs {SERVING} in a docker scope")
    if len(scopes) > 1:
        raise RuntimeError(f"{len(scopes)} docker scopes run {SERVING}: {', '.join(ident[:12] for _, ident in scopes)}")
    (path, ident) = scopes[0]
    pid = found[0][0]
    scope = cgroups / path.lstrip("/")

    def read(name: str) -> str:
        return (scope / name).read_text(encoding="utf-8").strip()

    limit = read("memory.max")
    # Field 22 of /proc/<pid>/stat, after the parenthesized name, which may hold spaces.
    started = int((proc / str(pid) / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()[19])
    uptime = float((proc / "uptime").read_text(encoding="utf-8").split()[0])
    return {
        "method": f"/proc/{pid}/cgroup of the process running {SERVING} names {path}",
        "container": ident[:12],
        "memory_current": int(read("memory.current")),
        "memory_peak": int(read("memory.peak")),
        "memory_max": None if limit == "max" else int(limit),
        "anon": keyed(read("memory.stat")).get("anon"),
        "cpu_usec": keyed(read("cpu.stat"))["usage_usec"],
        "age_s": round(uptime - started / os.sysconf("SC_CLK_TCK"), 1),
    }


def shown(unit: str) -> dict[str, str]:
    text = ran("systemctl", "show", unit, *(f"--property={name}" for name in PROPERTIES))
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def failed() -> list[str]:
    text = ran("systemctl", "list-units", "--state=failed", "--no-legend", "--plain", "severino-hq*")
    return [line.split()[0] for line in text.splitlines() if line.strip()]


def drift() -> dict:
    """The drift check's last run: how it ended and what it printed."""

    unit = shown(DRIFT)
    invocation = unit.get("InvocationID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        return {**unit, "said": []}
    said = ran("journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "-o", "cat", "--no-pager")
    return {**unit, "said": [line for line in said.splitlines() if line.strip()]}


def reading(read) -> dict:
    try:
        return {"value": read()}
    except Exception as error:  # noqa: BLE001 - any failure is that reading's row, never the document's
        return {"error": f"{type(error).__name__}: {error}"}


def readings(proc: Path = Path("/proc"), cgroups: Path = Path("/sys/fs/cgroup")) -> dict:
    return {
        "container": reading(lambda: container(proc, cgroups)),
        "units": {unit: reading(lambda unit=unit: shown(unit)) for unit in TIMED},
        "failed": reading(failed),
        "drift": reading(drift),
    }


if __name__ == "__main__":
    json.dump(readings(), sys.stdout)
    sys.stdout.write("\n")
