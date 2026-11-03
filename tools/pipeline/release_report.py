"""`mise run release:report [pages]... [--base <ref>]`: one release, after its Deploy job is green.

Writes var/release/<commit>.md for this checkout's HEAD: its performance beside
the release production ran before it (`perf --base`), production's memory and
CPU, and the deploy host's health (release_readings.py, run there over SSH).
Read-only everywhere: GitHub's deployments, the host as an unprivileged account.
No standard tool composes these, so this joins the commands that already give
each piece. A reading that fails is a row saying so.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hq.platform.core.tests.task import RefError, commit_of, usage
from tools.perf import report as perf_report
from tools.perf.table import SERVER, SUMMARY
from tools.pipeline.production import DEPLOYMENTS, Releases, gh, releases, statuses

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "release"
PERF = ROOT / "var" / "perf"
READINGS = Path(__file__).with_name("release_readings.py")
SHORT = 12
MB = 1024**2
# What fails a reading here: a command that exits non-zero, times out, or prints what is not JSON.
UNREAD = (OSError, ValueError, subprocess.TimeoutExpired)

Row = tuple[str, ...]


@dataclass
class Report:
    release: str
    base: str | None = None
    perf: list[Row] = field(default_factory=list)
    production: list[Row] = field(default_factory=list)
    host: list[Row] = field(default_factory=list)
    # Lines under the verdict: what the numbers were measured under.
    notes: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failed_units: list[str] = field(default_factory=list)
    # perf's word on the pages whose server time moved past its spread.
    moved: str = ""

    def fail(self, rows: list[Row], what: str, error: object) -> None:
        rows.append((what, f"**not read**: {error}"))
        self.failed.append(what)


# ------------------------------------------------------------------- perf


def mb(value: float | None) -> str:
    return "?" if value is None else f"{value / MB:.1f} MB"


def shown(value: float | None, places: int, unit: str = "") -> str:
    return "?" if value is None else f"{value:.{places}f}{f' {unit}' if unit else ''}"


def change(here: float | None, base: float | None, places: int) -> str:
    return "" if here is None or base is None else f"{here - base:+.{places}f}"


def totals(side: dict[str, Any]) -> dict[str, float | None]:
    """Each server column summed over every page, as perf's total row sums it."""

    found: dict[str, float | None] = {}
    for label, read, _ in SERVER:
        present = [value for entry in side["pages"] if (value := read(entry.get("server") or {})) is not None]
        found[label] = sum(present) if present else None
    return found


def headline(here: dict[str, Any], base: dict[str, Any]) -> list[Row]:
    """The before/after numbers: memory, cold start and the server's totals, each beside its move."""

    rows: list[Row] = []
    for label, read, places, unit in SUMMARY:
        try:
            mine, theirs = read(here), read(base)
        except KeyError, TypeError:
            rows.append((label, "?", "?", "not measured"))
            continue
        rows.append((label, shown(theirs, places, unit), shown(mine, places, unit), change(mine, theirs, places)))
    mine_totals, their_totals = totals(here), totals(base)
    for label, _, places in SERVER:
        mine, theirs = mine_totals[label], their_totals[label]
        rows.append(
            (f"{label}, every page summed", shown(theirs, places), shown(mine, places), change(mine, theirs, places))
        )
    return rows


def perf_rows(made: Report, doc: dict[str, Any]) -> None:
    if doc.get("alone"):
        made.notes.append(f"The host is measured alone on both sides: {doc['alone']}")
    for failure in perf_report.failures(doc):
        made.fail(made.perf, "perf", failure)
    here, base = doc.get("here"), doc.get("base")
    if not isinstance(here, dict) or not isinstance(base, dict):
        made.fail(made.perf, "perf", f"run {doc.get('run')} holds no report of both sides")
        return
    made.perf.extend(headline(here, base))
    made.moved = perf_report.verdict(here)
    made.perf.append(("server time, page by page", "", "", made.moved))
    if reused := perf_report.kept_from(doc):
        made.notes.append(f"{reused[0].upper()}{reused[1:]}")
    made.notes.append(f"Every page: var/perf/{doc['run']}.table")


def run_perf(made: Report, pages: list[str]) -> Path | None:
    """perf's run of ``pages`` beside the base, its output passed through, and the run it kept."""

    base = made.base or ""
    # The name perf gives this checkout, marked when it holds changes past the release.
    here = perf_report.commit_of(ROOT)
    if here != made.release[:SHORT]:
        made.notes.append(f"Measured as {here}: this checkout holds changes past the release")
    started = time.time()
    # The child parses its own arguments; these are this task's.
    env = {key: value for key, value in os.environ.items() if not key.startswith("usage_")}
    subprocess.run(["mise", "run", "perf", *pages, "--base", base], cwd=ROOT, env=env, check=False)
    # A base that refuses the extension set starts a second run, host alone: the newest is the one finished.
    kept = sorted(path for path in PERF.glob(f"{here}-vs-{base[:SHORT]}-*.json") if path.stat().st_mtime >= started)
    return kept[-1] if kept else None


# ------------------------------------------------------------------- the host


def host_readings(host: str) -> dict[str, Any]:
    with READINGS.open("rb") as source:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, "python3", "-I", "-"],
            stdin=source,
            capture_output=True,
            timeout=120,
            check=False,
        )
    if done.returncode:
        raise OSError(f"ssh exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}")
    return json.loads(done.stdout)


def taken(made: Report, rows: list[Row], what: str, read: dict[str, Any]) -> Any:
    """A reading's value, or None with its failure as the row."""

    if "error" in read:
        made.fail(rows, what, read["error"])
        return None
    return read["value"]


def number(text: str | None) -> int | None:
    return int(text) if text and text.isdigit() else None


def last_run(unit: dict[str, str]) -> str:
    """When a unit's last run started, how long it took, how it ended and what it used."""

    start = number(unit.get("ExecMainStartTimestampMonotonic"))
    end = number(unit.get("ExecMainExitTimestampMonotonic"))
    if not start:
        return "no run recorded since the host booted"
    took = "still running" if not end or end < start else f"{(end - start) / 1e6:.2f} s"
    said = f"{unit.get('ExecMainStartTimestamp')}, {took}, {unit.get('Result')} (exit {unit.get('ExecMainStatus')})"
    if (cpu := number(unit.get("CPUUsageNSec"))) is not None:
        said += f", CPU {cpu / 1e9:.2f} s"
    if (peak := number(unit.get("MemoryPeak"))) is not None:
        said += f", peak {mb(peak)}"
    return said


def production_rows(made: Report, read: dict[str, Any]) -> None:
    if (found := taken(made, made.production, "the HQ container", read)) is None:
        return
    limit = found.get("memory_max")
    seconds = found["cpu_usec"] / 1e6
    share = seconds / found["age_s"] * 100 if found["age_s"] else 0.0
    made.production += [
        ("container", f"{found['container']}, up {found['age_s'] / 60:.1f} min"),
        # anon is the reading perf takes of an image, so it leads; the cgroup's
        # totals also count page cache, which the limit can reclaim.
        ("anonymous memory now", mb(found.get("anon"))),
        ("cgroup memory now", mb(found["memory_current"]) + (f" of {mb(limit)}" if limit else ", no limit")),
        ("cgroup memory peak since it started", mb(found["memory_peak"])),
        ("CPU since it started", f"{seconds:.1f} s, {share:.1f}% of one core on average"),
        ("found by", found["method"]),
    ]


def host_rows(made: Report, read: dict[str, Any]) -> None:
    rows = made.host
    if (drift := taken(made, rows, "drift check", read["drift"])) is not None:
        verdict = drift["said"][-1] if drift["said"] else "printed nothing"
        rows.append(("drift check", f"{verdict} ({last_run(drift)})"))
    if (failed := taken(made, rows, "failed units", read["failed"])) is not None:
        made.failed_units = failed
        rows.append(("failed units", ", ".join(failed) or "none"))
    for unit, unit_read in read["units"].items():
        if (found := taken(made, rows, unit, unit_read)) is not None:
            rows.append((f"{unit}, last run", last_run(found)))


# ------------------------------------------------------------------- the report


def table(headers: Row, rows: list[Row]) -> Iterator[str]:
    yield "| " + " | ".join(headers) + " |"
    yield "|" + " --- |" * len(headers)
    for row in rows:
        padded = [*row, *[""] * (len(headers) - len(row))]
        yield "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in padded) + " |"


def standing(found: Releases | None) -> str:
    """How the release stands in production, as a clause of the verdict: nothing for a
    release that went live with every check passed, or when the deployments were not read."""

    if found is None:
        return ""
    if found.unknown_since:
        return f"; production's state is unknown: the rollback of {found.unknown_since[:SHORT]} failed"
    if not found.deployed:
        return f"; not deployed, production runs {(found.running or 'nothing recorded')[:SHORT]}"
    if found.check_failed:
        return "; live, and a check after its deploy failed (the Deploy job's log names it)"
    return ""


def verdict(made: Report, found: Releases | None) -> str:
    said = f"release {made.release[:SHORT]}"
    said += f" beside {made.base[:SHORT]}" if made.base else ", with no previous release to compare"
    if made.moved:
        said += f": {made.moved}"
    said += standing(found)
    if made.failed_units:
        said += f"; failed units: {', '.join(made.failed_units)}"
    if made.failed:
        count = len(made.failed)
        return said + f"; {count} reading{'s' * (count != 1)} failed ({', '.join(made.failed)})"
    return said + "; every reading taken"


def render(made: Report, found: Releases | None) -> str:
    closing = verdict(made, found)
    lines = [f"# Release {made.release[:SHORT]}", "", closing, ""]
    if made.notes:
        lines += [*(f"- {note}" for note in made.notes), ""]
    base = (made.base or "no base")[:SHORT]
    lines += ["## Before and after", "", *table(("measure", base, made.release[:SHORT], "moved"), made.perf)]
    lines += ["", "## Production", "", *table(("measure", "reading"), made.production)]
    lines += ["", "## Deploy host", "", *table(("check", "reading"), made.host)]
    return "\n".join([*lines, "", f"Verdict: {closing}", ""])


def base_of(made: Report, asked: str, found: Releases | None) -> str | None:
    if asked:
        try:
            return commit_of(asked)
        except RefError as refused:
            made.fail(made.perf, "base", refused)
            return None
    if found and found.previous:
        return found.previous
    made.fail(made.perf, "base", "no earlier production deployment was marked live")
    return None


def gather(made: Report) -> Releases | None:
    found = None
    try:
        found = releases(made.release, gh(DEPLOYMENTS), statuses)
    except UNREAD as failed:
        made.fail(made.production, "GitHub's deployments", failed)
    made.base = base_of(made, usage().text("base"), found)
    if made.base:
        kept = run_perf(made, list(usage().repeated("pages")) or ["all"])
        if kept is None:
            made.fail(made.perf, "perf", "it kept no run; its output above says why")
        else:
            perf_rows(made, json.loads(kept.read_text(encoding="utf-8")))
    try:
        read = host_readings(os.environ["SEVERINO_HQ_DEPLOY_HOST"])
    except UNREAD as failed:
        made.fail(made.production, "the deploy host", failed)
    else:
        production_rows(made, read["container"])
        host_rows(made, read)
    return found


def main() -> int:
    """0 when every reading was taken of a release production runs with every check passed; 1 otherwise, the report written either way."""

    made = Report(commit_of("HEAD"))
    found = gather(made)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{made.release[:SHORT]}.md"
    path.write_text(render(made, found), encoding="utf-8")
    print(f"{verdict(made, found)}; written to {path.relative_to(ROOT)}")
    return 1 if made.failed or standing(found) else 0


if __name__ == "__main__":
    sys.exit(main())
