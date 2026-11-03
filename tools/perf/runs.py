"""A perf run as it is kept: var/perf/<run>.json, rewritten as each measurement finishes.

A run is named by both commits and the target, and the second it started, so
no run overwrites another of the same commit. Its document holds each side's
report under `base` and `here`, each carrying the plan it was measured under
the failures it met and the load it was measured under, so a later run asking
the same plan of the same commit under a load alike can take a complete report
instead of measuring it again.
"""

import copy
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Report = dict[str, Any]
# How a report's numbers are computed from what it measured. A kept report is taken
# only under the same, so a number never stands beside one computed another way:
# raise it when a computation changes.
FORMAT = 3
# How far apart, in one-minute load average, a kept report's load and this run's may be for it to
# stand in. Cold start swings most with load, by more than a change moves it: within 2, a kept
# base stands in on a machine as quiet or as busy as when it was measured, and one measured idle
# never stands beside a tip measured with the cores oversubscribed, or the other way round.
LOAD_BOUND = 2.0


def name(here: str, base: str | None, target: str, at: datetime) -> str:
    sides = f"{here}-vs-{base}" if base else here
    return f"{sides}-{target.replace(':', '-')}-{at.astimezone(UTC):%Y%m%dT%H%M%SZ}"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Run:
    """One run's document and the file it is kept in."""

    path: Path
    doc: dict[str, Any]

    @classmethod
    def start(cls, out: Path, stem: str, **fields: Any) -> Run:
        """A new run's file, claimed under ``stem`` or, when that is taken, the first free ``stem-<n>``."""

        out.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 100):
            path = out / (f"{stem}.json" if attempt == 1 else f"{stem}-{attempt}.json")
            try:
                with path.open("x", encoding="utf-8"):
                    pass
            except FileExistsError:
                continue
            run = cls(path, {"run": path.stem, "started_at": now(), **fields})
            run.save()
            return run
        raise FileExistsError(f"every name for {stem} under {out} is taken")

    @property
    def name(self) -> str:
        return self.path.stem

    def save(self) -> None:
        """The document as it stands, replacing the file whole so a reader never meets half of it."""

        partial = self.path.with_name(self.path.name + ".partial")
        partial.write_text(json.dumps(self.doc, indent=2) + "\n", encoding="utf-8")
        partial.replace(self.path)


def side(commit: str, plan: dict[str, Any]) -> Report:
    """A report about to be measured: what was asked of it, and room for what it finds."""

    return {
        "commit": commit,
        "target": plan["target"],
        "requests": plan["requests"],
        "widths": plan["widths"],
        "plan": plan,
        "format": FORMAT,
        "measured_at": now(),
        "finished_at": None,
        "failures": [],
        "pages": [],
    }


def complete(report: Report) -> bool:
    return bool(report.get("finished_at")) and not report.get("failures")


def _reports(out: Path) -> Iterator[tuple[str, Report]]:
    for path in sorted(out.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        for key in ("base", "here"):
            if isinstance(report := doc.get(key), dict):
                yield path.stem, report


def _covers(kept: dict[str, Any], plan: dict[str, Any]) -> bool:
    """Whether a report kept under ``kept`` was asked all ``plan`` asks: the same of every page, of
    each page ``plan`` names."""

    def alike(asked: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in asked.items() if key != "pages"}

    return alike(kept) == alike(plan) and set(plan["pages"]) <= set(kept.get("pages", ()))


def _taking(report: Report, plan: dict[str, Any]) -> Report:
    """``report`` holding only the pages ``plan`` names, in its order, as measured under ``plan``."""

    entries = {entry["page"]: entry for entry in report["pages"]}
    return {**report, "plan": plan, "pages": [entries[page] for page in plan["pages"]]}


def load() -> float:
    """The machine's one-minute load average, the load a side's numbers are measured under."""

    return round(os.getloadavg()[0], 2)


def alike(kept: Any, now: float) -> bool:
    """Whether a report measured at load ``kept`` may stand beside numbers measured at ``now``; a
    report that kept no load stands beside nothing."""

    return isinstance(kept, int | float) and abs(kept - now) <= LOAD_BOUND


def saved(out: Path, commit: str, plan: dict[str, Any], at_load: float) -> tuple[str, Report] | None:
    """The latest complete report of ``commit`` in this ``FORMAT``, measured under a plan covering
    ``plan`` and a load alike ``at_load``, holding just its pages, and the run it is in."""

    found = [
        (run, report)
        for run, report in _reports(out)
        if report.get("commit") == commit and report.get("format") == FORMAT and isinstance(report.get("plan"), dict)
        if complete(report)
        if _covers(report["plan"], plan)
        if alike(report.get("load"), at_load)
    ]
    latest = max(found, key=lambda pair: pair[1]["finished_at"], default=None)
    return latest and (latest[0], _taking(latest[1], plan))


def stand_in(measured: Report, kept: Report) -> None:
    """``measured``, a base this run asked for server time alone, given ``kept``'s other numbers,
    each one ``report.measure_costs`` writes, so no number of this run's is ever ``kept``'s."""

    for name in ("cold_start", "memory", "load"):
        measured[name] = copy.deepcopy(kept[name])
    entries = {entry["page"]: entry for entry in kept["pages"]}
    for entry in measured["pages"]:
        was = entries[entry["page"]]
        entry["browser"] = copy.deepcopy(was.get("browser", {}))
        if "queries" in was.get("server", {}):
            entry["server"]["queries"] = was["server"]["queries"]
