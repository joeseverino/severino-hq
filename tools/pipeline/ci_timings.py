"""Where a CI run's time went: each job's and step's seconds, and the critical path.

    mise run ci:timings <run>

Read-only: one `gh run view <run> --json jobs`. The critical path is read off
the clock, since the run's record carries no `needs`: from the job that
finished last, back through the job that finished last before it started,
which is the one it waited for.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Span:
    name: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(frozen=True)
class Job(Span):
    steps: tuple[Span, ...]


def when(text: str) -> datetime:
    """GitHub's one shape of instant; HQ's own parser is not importable here."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def ran(record: dict) -> bool:
    """A job or step that ran: a skipped one has no time of its own."""
    return record.get("conclusion") != "skipped" and bool(record.get("startedAt")) and bool(record.get("completedAt"))


def jobs(record: dict) -> list[Job]:
    found = [
        Job(
            job["name"],
            when(job["startedAt"]),
            when(job["completedAt"]),
            tuple(
                Span(step["name"], when(step["startedAt"]), when(step["completedAt"]))
                for step in job.get("steps", [])
                if ran(step)
            ),
        )
        for job in record.get("jobs", [])
        if ran(job)
    ]
    return sorted(found, key=lambda job: (job.start, job.name))


def critical_path(found: list[Job]) -> list[Job]:
    if not found:
        return []
    path = [max(found, key=lambda job: job.end)]
    while True:
        before = [job for job in found if job.end <= path[0].start]
        if not before:
            return path
        path.insert(0, max(before, key=lambda job: job.end))


def report(found: list[Job]) -> str:
    if not found:
        return "No job in this run has run.\n"
    origin = min(job.start for job in found)
    lines = []
    for job in found:
        lines.append(f"{job.seconds:5d}s  {job.name}  (+{int((job.start - origin).total_seconds())}s)")
        lines.extend(f"         {step.seconds:5d}s  {step.name}" for step in job.steps)
    path = critical_path(found)
    wall = int((path[-1].end - origin).total_seconds())
    lines.append("")
    lines.append("Critical path: " + " -> ".join(f"{job.name} {job.seconds}s" for job in path) + f" = {wall}s")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: ci_timings.py <run>", file=sys.stderr)
        return 2
    answer = subprocess.run(
        ["gh", "run", "view", argv[0], "--json", "jobs"], capture_output=True, text=True, check=False
    )
    if answer.returncode != 0:
        print(answer.stderr.strip(), file=sys.stderr)
        return 1
    sys.stdout.write(report(jobs(json.loads(answer.stdout))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
