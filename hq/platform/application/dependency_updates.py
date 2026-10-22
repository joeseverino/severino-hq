"""Which repositories HQ's dependency update run covers, and when it is due.

A project is opted in by ``project.dependency_updates``. HQ holds no
credential, so it only plans: ``plan`` tells the controller each opted-in
repository, grouped by the account that owns it, and whether a run is due.
The controller mints one token per account, scoped to exactly those
repositories and the App's Renovate grant, and hands it to root, which runs
Renovate (``controller/providers/github_dependency_run.go``). How each run
ended comes back as the ``github.dependency_run`` reading.

A run is due once a day and when somebody asks (``request_run``). The clock is
the latest run the reading holds, a hand-off included, so a run that root has
not finished yet is not handed off again.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import (
    DEPENDENCY_RUN_EVERY,
    DEPENDENCY_RUN_KIND as KIND,
    REPOSITORY,
)
from hq.domains.projects.models import Project

from .domains import records_of
from .projection import iso, read_once
from .security import Principal
lazy from .cadence import forced_reads, request_reads
lazy from .projects import repository_key
lazy from .timestamps import moment

RUN_NOW = "project.dependency_updates.run"
# A hand-off that failed is tried again after this, not on every sweep.
RETRY_AFTER = timedelta(hours=1)
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


@dataclass(frozen=True, slots=True)
class Run:
    """How the latest run that covered a repository went, as root reported it."""

    # ``ok``, ``failed``, ``timeout``, or ``handed_off``: still running.
    outcome: str
    started_at: datetime
    finished_at: datetime | None

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
        run = Run(str(record.get("outcome", "")), started, moment(record.get("finished_at")))
        for name in record.get("repositories") or ():
            key = str(name).lower()
            if key not in found or found[key].started_at < started:
                found[key] = run
    return found


def asked_at() -> datetime | None:
    """When a run was asked for and has not been handed off yet."""

    return next((read.requested_at for read in forced_reads() if read.kind == KIND), None)


def plan(now: datetime | None = None) -> dict[str, Any]:
    """Which repositories the run covers, by owner, and whether to run now.

    Due when somebody asked, or when the latest run is a day old. A hand-off
    that failed was an attempt, so it is not made again on every sweep.
    ``last_started_at`` is that latest run: a result the controller reads is
    news only when it started after it.
    """

    covered = owners()
    snapshot = _snapshot()
    started = last_started(snapshot)
    found = {
        "owners": [{"owner": owner, "repositories": names} for owner, names in covered.items()],
        "due": False,
        "last_started_at": started.isoformat() if started is not None else "",
    }
    if not covered:
        return found
    now = now or timezone.now()
    if asked_at() is not None:
        due = True
    elif snapshot is not None and not snapshot.reachable and now - snapshot.updated_at < RETRY_AFTER:
        due = False
    else:
        due = started is None or now - started >= DEPENDENCY_RUN_EVERY
    return {**found, "due": due}


@dataclass(frozen=True, slots=True)
class DependencyRunCommand:
    """A run of every opted-in repository now."""


def request_run(_command: DependencyRunCommand, *, principal: Principal) -> dict[str, Any]:
    """Ask the controller to hand off a run now, and wake it.

    The ask is a read of ``github.dependency_run``: the controller's next pass
    finds the plan due, hands off, and reports the hand-off, which answers it.
    """

    principal.require(records_of("projects").write)
    if not owners():
        return {
            "ok": False,
            "error": {"code": "precondition", "message": "No project is opted in to dependency updates."},
        }
    request_reads((KIND,), principal=principal)
    asked = asked_at() or timezone.now()
    return {"ok": True, "requested": True, "asked_at": asked.isoformat()}
