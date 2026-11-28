"""Which repositories HQ's dependency update run covers, and when it is due.

HQ holds no credential, so it only plans: ``plan`` tells the controller each
repository a project opts in to (``project.dependency_updates.set``), by owner, and
whether a run is due, once a day and on ``request_run``. Open PR asks a run of
one repository instead, with the branch Renovate is to open in it
(``request_open``). The controller runs it
(``controller/providers/github_dependency_run.go``), and the hand-off and the
reading each run comes back as, with each branch its report names, are
docs/Derived-Facts.md#dependency-updates.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import DEPENDENCY_RUN_KIND as KIND, REPOSITORY
from hq.domains.jobs.models import JobState
from hq.domains.projects.models import Project

from .asks import AskedAnswer, asked_read
from .freshness import RETRY_AFTER, freshness
from .projection import iso, read_once
from .refusal_codes import Code
from .refusal_status import RefusalEnvelope, refuse
from .security import Principal
from .tone import Tone
lazy from .cadence import ask
lazy from .capabilities import command_spec
lazy from .moments import moment
lazy from .projects import repository_key
lazy from .read_requests import forced_reads

RUN_NOW = "project.dependency_updates.run"
# Each outcome a run reports, as the job state every page words and tones.
SEEN = {"ok": JobState.SUCCEEDED, "handed_off": JobState.RUNNING, "timeout": JobState.LOST}


def seen(outcome: str) -> JobState:
    """A run's reported outcome as the job state every page words and tones."""

    return SEEN.get(outcome, JobState.FAILED)


_REPOSITORY = re.compile(REPOSITORY)


def owners() -> dict[str, list[str]]:
    """Each opted-in repository as ``owner/name``, by its owner, both sorted.

    A project whose repository is not a GitHub repository is not covered.
    """

    found: dict[str, set[str]] = {}
    for url in Project.objects.filter(dependency_updates=True).values_list("repository_url", flat=True):
        if matched := _REPOSITORY.match(repository_key(url)):
            found.setdefault(matched[1], set()).add(matched[0])
    return {owner: sorted(found[owner]) for owner in sorted(found)}


def _snapshot() -> ProviderInventory | None:
    return ProviderInventory.objects.filter(kind=KIND).first()


def last_started(snapshot: ProviderInventory | None) -> datetime | None:
    """When the latest run the reading holds started, or was handed off."""

    records = snapshot.records if snapshot is not None else ()
    return max(filter(None, (moment(record.get("started_at")) for record in records)), default=None)


# What Renovate waits for before it opens a branch, by the result its report
# gives one it holds (its ``BranchResult``), as every page words it.
HELD = {
    "needs-approval": "until it is approved",
    "needs-pr-approval": "until its pull request is approved",
    "pending": "until its release is old enough",
    "not-scheduled": "until its schedule allows",
    "update-not-scheduled": "until its schedule allows",
    "pr-limit-reached": "while it is at its pull request limit",
    "branch-limit-reached": "while it is at its branch limit",
    "commit-per-run-limit-reached": "while it is at its commit limit",
    "commit-hourly-limit-reached": "while it is at its commit limit",
    "minimum-group-size-not-met": "until its group is large enough",
}


@dataclass(frozen=True, slots=True)
class Branch:
    """One branch of a repository as the run's report says Renovate left it."""

    branch: str
    result: str
    # The number of the pull request it has, None for none.
    pull_request: int | None
    # The package files its upgrades change.
    files: tuple[str, ...]

    @property
    def held(self) -> bool:
        """Whether Renovate holds it back, with no pull request."""

        return self.pull_request is None and self.result in HELD

    @property
    def until(self) -> str:
        """What Renovate waits for before it opens it, "" where it waits for nothing."""

        return HELD.get(self.result, "") if self.held else ""


@dataclass(frozen=True, slots=True)
class Run:
    """How the latest run that covered a repository went, as root reported it."""

    # ``ok``, ``failed``, ``timeout``, or ``handed_off``: still running.
    outcome: str
    started_at: datetime
    finished_at: datetime | None
    # Each branch of the repository the run's report names.
    branches: tuple[Branch, ...] = ()

    @property
    def seen(self) -> JobState:
        return seen(self.outcome)

    @property
    def tone(self) -> Tone:
        return self.seen.tone

    def as_record(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "started_at": iso(self.started_at), "finished_at": iso(self.finished_at)}


def last_run(repository: str) -> Run | None:
    """The latest run that covered ``repository`` (a URL or ``owner/name``).

    None when no run the reading holds named it. Read once per projection.
    """

    key = repository_key(repository)
    return read_once("dependency_updates.last_runs", _last_runs).get(key) if key else None


def _last_runs() -> dict[str, Run]:
    found: dict[str, Run] = {}
    records = ProviderInventory.objects.filter(kind=KIND).values_list("records", flat=True).first()
    for record in records or ():
        started = moment(record.get("started_at"))
        if started is None:
            continue
        for name in record.get("repositories") or ():
            key = str(name).lower()
            if key not in found or found[key].started_at < started:
                branches = tuple(
                    Branch(
                        str(each.get("branch", "")),
                        str(each.get("result", "")),
                        each.get("pull_request"),
                        tuple(map(str, each.get("files") or ())),
                    )
                    for each in record.get("branches") or ()
                    if str(each.get("repository", "")).lower() == key
                )
                found[key] = Run(str(record.get("outcome", "")), started, moment(record.get("finished_at")), branches)
    return found


def _asked() -> tuple[tuple[str, datetime], ...]:
    """What of the run each pending ask names, oldest first, with when it was
    asked: "" for every opted-in repository, else a repository, or a
    repository and a branch of it as ``owner/name@branch``. An ask of another
    subject, such as the run's own unit telling HQ it ended, reads the run's
    outcome and asks no run."""

    return tuple((read.subject, read.requested_at) for read in forced_reads() if read.kind == KIND)


def _subject(subject: str) -> tuple[str, str]:
    """An ask's repository and branch, each "" where it names none."""

    repository, _, branch = subject.partition("@")
    return repository.lower(), branch


def asked_at(repository: str = "") -> datetime | None:
    """When a run was asked for and has not been handed off yet: a run of
    every repository, or one of ``repository`` (a URL or ``owner/name``)."""

    key = repository_key(repository) if repository else ""
    return next((at for subject, at in _asked() if subject == "" or (key and _subject(subject)[0] == key)), None)


def plan(now: datetime | None = None) -> dict[str, Any]:
    """Which repositories the run covers, by owner, and whether to run now.

    Due when somebody asked, or when the oldest repository's latest run is a
    day old. A hand-off that failed was an attempt, so it is not made again on
    every sweep. Open PR's ask is a run of its one repository, with the
    branches asked of it, one repository an owner (``_scoped``).
    ``last_started_at`` is the latest run: a result the controller reads is
    news only when it started after it.
    """

    covered = owners()
    snapshot = _snapshot()
    found = {
        "owners": [{"owner": owner, "repositories": names, "branches": []} for owner, names in covered.items()],
        "due": False,
        "last_started_at": iso(last_started(snapshot)) or "",
    }
    if not covered:
        return found
    asked = [subject for subject, _at in _asked()]
    if scoped := _scoped(covered, asked):
        return {**found, "owners": scoped, "due": True}
    now = now or timezone.now()
    if "" in asked:
        due = True
    elif snapshot is not None and not snapshot.reachable and now - snapshot.updated_at < RETRY_AFTER:
        due = False
    else:
        runs = _last_runs()
        started = [runs[name].started_at if name in runs else None for names in covered.values() for name in names]
        due = freshness(KIND, None if None in started else min(filter(None, started)), now).due
    return {**found, "due": due}


def _scoped(covered: dict[str, list[str]], asked: list[str]) -> list[dict[str, Any]]:
    """Each owner's run Open PR asked, oldest ask first: one opted-in
    repository and the branches asked of it, since Renovate checks a branch
    by name in every repository it is given."""

    found: dict[str, dict[str, Any]] = {}
    for subject in asked:
        repository, branch = _subject(subject)
        owner = repository.partition("/")[0]
        if repository not in covered.get(owner, ()):
            continue
        run = found.setdefault(owner, {"owner": owner, "repositories": [repository], "branches": []})
        if run["repositories"] == [repository] and branch and branch not in run["branches"]:
            run["branches"].append(branch)
    return list(found.values())


@dataclass(frozen=True, slots=True)
class DependencyRunCommand:
    """A run of every opted-in repository now."""


def request_run(_command: DependencyRunCommand, *, principal: Principal) -> AskedAnswer | RefusalEnvelope:
    """Ask the controller to hand off a run now, and wake it.

    The ask is a read of ``github.dependency_run``: the controller's next pass
    finds the plan due, hands off, and reports the hand-off, which answers it.
    """

    del principal
    if not owners():
        return refuse(Code.PRECONDITION, "No project is opted in to dependency updates.")
    at = ask(reads=(KIND,), activity=True)
    return asked_read((KIND,), at, message=command_spec(RUN_NOW).requested()).answer()


def request_open(repository: str, branch: str, *, message: str) -> AskedAnswer | RefusalEnvelope:
    """Ask a run of ``repository`` alone, which has Renovate open ``branch``
    in it as if its box were ticked; with no branch, a run that sees its
    newer releases. The ask is the read ``request_run``'s is, of that subject."""

    key = repository_key(repository)
    if key not in owners().get(key.partition("/")[0], ()):
        return refuse(Code.PRECONDITION, f"{repository} is not opted in to dependency updates.")
    at = ask(reads=(KIND,), subject=f"{key}@{branch}" if branch else key, activity=True)
    return asked_read((KIND,), at, message=message).answer()
