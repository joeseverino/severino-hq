"""A perf:report run as it is kept: var/perf/<run>.json, rewritten as each measurement finishes.

A run is named by both commits and the target, and the second it started, so
no run overwrites another of the same commit. Its document holds each side's
report under `base` and `here`, each carrying the plan it was measured under
and the failures it met, so a later run asking the same plan of the same commit
can take a complete report instead of measuring it again.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Report = dict[str, Any]


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


def saved(out: Path, commit: str, plan: dict[str, Any]) -> tuple[str, Report] | None:
    """The latest complete report of ``commit`` measured under ``plan``, and the run it is in."""

    found = [
        (run, report)
        for run, report in _reports(out)
        if report.get("commit") == commit and report.get("plan") == plan and complete(report)
    ]
    return max(found, key=lambda pair: pair[1]["finished_at"], default=None)
