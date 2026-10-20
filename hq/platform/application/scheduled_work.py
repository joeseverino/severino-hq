"""Work the host asks HQ for by name, declared once.

A unit asks for one of these by name over the bridge socket and the running
process does it as a job (``hq.domains.jobs``): one at a time, with a row that
says how it ended and an audit entry. Work that declares ``every`` is done on
that cadence: the one hourly timer asks for ``due``, which does each piece of
the host's and the extensions' work whose cadence has come round. Work without
one is asked for by a unit of its own (``units.read``, through ``OnFailure=``).
What the work is, and how often, is declared here and nowhere else. A unit
naming anything else is refused, and a test holds the shipped units to this
list.

HQ starts the same work itself when it learns something is due sooner than the
timer would find it (``start``).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone
lazy from django.contrib.sessions.models import Session
lazy from django.core.management import call_command

lazy from hq.domains.content.content_sync import ContentSyncError, sync_content_index
lazy from hq.domains.control_plane.observations.host import UNIT_KIND
lazy from hq.domains.jobs import runner
lazy from hq.domains.jobs.runner import Failed, JobConflict, due
lazy from hq.platform.core.audit import prune_routine, record_operation
lazy from hq.platform.core.database import optimize
lazy from hq.platform.core.models import AuditLog

from .ui import counted
lazy from . import outbound_work
lazy from .asks import job_standing
lazy from .cadence import request_reading
lazy from .outbound_work import run_now
lazy from .public_registry import refresh
lazy from .security import cli_principal, internal_principal


@dataclass(frozen=True, slots=True)
class ScheduledWork:
    """``name`` is the job's kind; ``run`` is called with the job's
    ``Progress`` and returns what the job stores; ``every`` is how often it is
    done, by the ``due`` timer, or None for work a unit asks for itself."""

    name: str
    label: str
    run: Callable[[Any], dict[str, Any] | None]
    every: timedelta | None = None


def prune_audit(progress: Any = None) -> dict[str, Any]:
    """Delete routine audit events past their window, on the record."""

    days = int(settings.SEVERINO_AUDIT_ROUTINE_DAYS)
    deleted = prune_routine(days=days)
    if deleted:
        record_operation(
            "audit.prune",
            f"Deleted {counted(deleted, 'routine event', 'routine events')} older than {counted(days, 'day')}.",
            action=AuditLog.Action.DELETED,
            metadata={"deleted": deleted, "days": days},
        )
    # The one job that holds the database every day is when SQLite refreshes
    # its planner statistics.
    optimize()
    return {"deleted": deleted, "days": days}


def _clear_sessions(progress: Any) -> dict[str, Any]:
    """A session row outlives its expiry until something deletes it."""

    before = Session.objects.count()
    call_command("clearsessions")
    return {"deleted": before - Session.objects.count()}


def _content_index(progress: Any) -> dict[str, Any]:
    try:
        return sync_content_index()
    except ContentSyncError as exc:
        raise Failed(str(exc)) from exc


def _public_registry(progress: Any) -> dict[str, Any]:
    return refresh(principal=cli_principal())


def _unit_state(progress: Any) -> dict[str, Any]:
    """Have the controller read the machine's units now: a unit there failed.

    The failed unit is not named and need not be. The reading says which.
    """

    return {"asked": UNIT_KIND, "rung": request_reading(UNIT_KIND)}


def _due(progress: Any = None) -> dict[str, Any]:
    """Do the host's and the extensions' work whose cadence has come round,
    each as its own job, so each says how it ended."""

    now = timezone.now()
    ended: dict[str, str] = {
        work.name: run(work.name)["state"]
        for work in SCHEDULED
        if work.every is not None and due(work.name, work.every, now=now)
    }
    principal = internal_principal(ACTOR)
    for work in outbound_work.due(now):
        try:
            ended[work.name] = run_now(work.name, principal=principal).state
        except JobConflict:
            ended[work.name] = "running"
        except ValueError as refused:
            ended[work.name] = f"refused: {refused}"
    return {"due": len(ended), "ended": ended}


DAILY = timedelta(days=1)

SCHEDULED: tuple[ScheduledWork, ...] = (
    ScheduledWork("audit.prune", "Delete old routine log entries", prune_audit, every=DAILY),
    ScheduledWork("content.sync", "Fetch the site's content list", _content_index, every=DAILY),
    ScheduledWork("due", "Do the work that is due", _due),
    ScheduledWork("registry.refresh", "Read domain registration and image data", _public_registry, every=DAILY),
    ScheduledWork("sessions.clear", "Delete expired sessions", _clear_sessions, every=DAILY),
    ScheduledWork("units.read", "Check HQ's own services after a failure", _unit_state),
)

ACTOR = "timer"


def job_names() -> dict[str, str]:
    """Each declared job's name in words, by its kind: the host's scheduled
    work and the outbound work the composition declares."""

    return {
        **{work.name: work.label for work in SCHEDULED},
        **{name: work.label for name, work in outbound_work.declared_work().items()},
    }


def named(name: str) -> ScheduledWork:
    for work in SCHEDULED:
        if work.name == name:
            return work
    raise ValueError(f"No scheduled work named {name!r} is declared.")


def run(name: str) -> dict[str, Any]:
    """Do the work to its end on the calling thread and say how it ended.

    A live job of the same kind is the answer, not a failure: the work is
    being done.
    """

    work = named(name)
    try:
        job = runner.run(work.name, work.label, work.run, actor=ACTOR)
    except runner.JobConflict:
        return {"name": name, "state": "running", "note": f"{work.label} is already running."}
    return {"name": name, "state": job.state, "note": job_standing(job).note, "job": str(job.pk)}


def start(name: str) -> bool:
    """Start the work on a thread of its own; False when it is already running."""

    work = named(name)
    try:
        runner.start(work.name, work.label, work.run, actor=ACTOR)
    except runner.JobConflict:
        return False
    return True
