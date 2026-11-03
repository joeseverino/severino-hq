"""Where a CI run's time went: each job's and step's seconds, how long after its
last step each job was marked done, and the critical path.

`mise run audit:ci-timings <run>...`, read-only:
one `gh run view <run> --json jobs` a run.
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

    @property
    def end_lag(self) -> int:
        """Seconds between the job's last step and GitHub marking it done: the
        platform's time, which no step of the repository's spent."""

        if not self.steps:
            return 0
        return int((self.end - max(step.end for step in self.steps)).total_seconds())


def when(text: str) -> datetime:
    """GitHub's one shape of instant; HQ's own parser is not importable here."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")


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
    """Read off the clock, since the run's record carries no `needs`: from the job
    that finished last, back through the job that finished last before it
    started, which is the one it waited for."""

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
        offset = int((job.start - origin).total_seconds())
        lines.append(f"{job.seconds:5d}s  {job.name}  (+{offset}s, end lag {job.end_lag}s)")
        lines.extend(f"         {step.seconds:5d}s  {step.name}" for step in job.steps)
    path = critical_path(found)
    wall = int((path[-1].end - origin).total_seconds())
    lines.append("")
    chain = " -> ".join(f"{job.name} {job.seconds}s" for job in path)
    lag = sum(job.end_lag for job in path)
    lines.append(f"Critical path: {chain} = {wall}s, {lag}s of it end lag")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    """Each run's report, headed by its id when there are several. A word that is
    not a run id is refused before gh runs, so none can reach it as a flag."""

    if not argv:
        print("usage: ci_timings.py <run>...", file=sys.stderr)
        return 2
    for run in argv:
        if not (run.isascii() and run.isdigit()):
            print(f"ci_timings: {run} is not a run id, the number gh run list prints", file=sys.stderr)
            return 2
    for index, run in enumerate(argv):
        answer = subprocess.run(
            ["gh", "run", "view", run, "--json", "jobs"], capture_output=True, text=True, check=False
        )
        if answer.returncode != 0:
            print(answer.stderr.strip(), file=sys.stderr)
            return 1
        if len(argv) > 1:
            sys.stdout.write(("\n" if index else "") + f"Run {run}\n")
        sys.stdout.write(report(jobs(json.loads(answer.stdout))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
