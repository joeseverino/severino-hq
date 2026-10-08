"""Work the host asks HQ for by name, declared once.

A unit asks for one of these by name over the bridge socket and the running
process does it as a job (``hq.domains.jobs``): one at a time, with a row that
says how it ended and an audit entry. A timer asks on its schedule, and a unit
that failed asks through ``OnFailure=``. When it is asked is the unit's; what
the work is, and that it exists, is declared here and nowhere else. A unit
naming anything else is refused, and a test holds the shipped units to this
list.

HQ starts the same work itself when it learns something is due sooner than the
timer would find it (``start``).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from .ui import counted


@dataclass(frozen=True, slots=True)
class ScheduledWork:
    """``name`` is the job's kind and what a timer asks for; ``run`` is called
    with the job's ``Progress`` and returns what the job stores."""

    name: str
    label: str
    run: Callable[[Any], dict[str, Any] | None]


def prune_audit(progress: Any = None) -> dict[str, Any]:
    """Delete routine audit events past their window, on the record."""

    from hq.platform.core.audit import prune_routine, record_operation
    from hq.platform.core.database import optimize
    from hq.platform.core.models import AuditLog

    days = int(settings.SEVERINO_AUDIT_ROUTINE_DAYS)
    deleted = prune_routine(days=days)
    if deleted:
        record_operation(
            "audit.prune",
            f"Deleted {counted(deleted, 'routine event', 'routine events')} older than "
            f"{counted(days, 'day')}.",
            action=AuditLog.Action.DELETED,
            metadata={"deleted": deleted, "days": days},
        )
    # The one job that holds the database every day is when SQLite refreshes
    # its planner statistics.
    optimize()
    return {"deleted": deleted, "days": days}


def _clear_sessions(progress: Any) -> dict[str, Any]:
    """A session row outlives its expiry until something deletes it."""

    from django.contrib.sessions.models import Session
    from django.core.management import call_command

    before = Session.objects.count()
    call_command("clearsessions")
    return {"deleted": before - Session.objects.count()}


def _contacts_inbox(progress: Any) -> dict[str, Any]:
    from hq.domains.contacts import inbox

    inbox.refresh(force=True)
    count, status = inbox.unread()
    return {"unread": count, "status": status}


def _content_index(progress: Any) -> dict[str, Any]:
    from hq.domains.content.content_sync import ContentSyncError, sync_content_index
    from hq.domains.jobs.runner import Failed

    try:
        return sync_content_index()
    except ContentSyncError as exc:
        raise Failed(str(exc)) from exc


def _public_registry(progress: Any) -> dict[str, Any]:
    from .public_registry import refresh
    from .security import cli_principal

    return refresh(principal=cli_principal())


def _unit_state(progress: Any) -> dict[str, Any]:
    """Have the controller read the machine's units now: a unit there failed.

    The failed unit is not named and need not be. The reading says which.
    """

    from hq.domains.control_plane.observations.host import UNIT_KIND

    from .cadence import request_reading

    return {"asked": UNIT_KIND, "rung": request_reading(UNIT_KIND)}


SCHEDULED: tuple[ScheduledWork, ...] = (
    ScheduledWork("audit.prune", "Delete old routine log entries", prune_audit),
    ScheduledWork("contacts.inbox", "Read the contact inbox", _contacts_inbox),
    ScheduledWork("content.sync", "Fetch the site's content list", _content_index),
    ScheduledWork("registry.refresh", "Read domain registration and image data", _public_registry),
    ScheduledWork("sessions.clear", "Delete expired sessions", _clear_sessions),
    ScheduledWork("units.read", "Check HQ's own services after a failure", _unit_state),
)

ACTOR = "timer"


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

    from hq.domains.jobs import runner

    from .asks import job_standing

    work = named(name)
    try:
        job = runner.run(work.name, work.label, work.run, actor=ACTOR)
    except runner.JobConflict:
        return {"name": name, "state": "running", "note": f"{work.label} is already running."}
    return {"name": name, "state": job.state, "note": job_standing(job).note, "job": str(job.pk)}


def start(name: str) -> bool:
    """Start the work on a thread of its own; False when it is already running."""

    from hq.domains.jobs import runner

    work = named(name)
    try:
        runner.start(work.name, work.label, work.run, actor=ACTOR)
    except runner.JobConflict:
        return False
    return True
