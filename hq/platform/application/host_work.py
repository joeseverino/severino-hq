"""The host's own background work, declared as any outbound work is.

Nobody is offered it as a capability: the hourly ``due`` timer does each piece
whose cadence has come round, a unit asks for the rest by name over the bridge
socket (``outbound_work.run_for_host``), and HQ starts one itself when it
learns something is due sooner. A test holds the shipped units to this list.
"""

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone
lazy from django.contrib.sessions.models import Session
lazy from django.core.management import call_command

lazy from hq.domains.content.content_sync import ContentSyncError, sync_content_index
lazy from hq.domains.control_plane.models import InboundSource
lazy from hq.domains.control_plane.observations.host import UNIT_FAILED
lazy from hq.domains.jobs import runner
lazy from hq.domains.jobs.runner import Failed
lazy from hq.platform.core.audit import prune_routine, record_operation
lazy from hq.platform.core.database import optimize
lazy from hq.platform.core.models import AuditLog

from .outbound_work import OutboundWork
from .record_errors import RecordConflict
from .ui import counted
lazy from . import approvals, outbound_work, public_registry
lazy from .automation_adoption import prune_proposals
lazy from .inbound import apply_events, prune_events
lazy from .public_registry import refresh


def prune_audit(_progress: Any = None, *, subject: str = "", principal: Any = None) -> dict[str, Any]:
    """Delete routine audit events, and the procedure proposals nothing
    still needs, past the one routine window, on the record."""

    del subject, principal
    days = int(settings.SEVERINO_AUDIT_ROUTINE_DAYS)
    deleted = prune_routine(days=days)
    if deleted:
        record_operation(
            "audit.prune",
            f"Deleted {counted(deleted, 'routine event', 'routine events')} older than {counted(days, 'day')}.",
            action=AuditLog.Action.DELETED,
            metadata={"deleted": deleted, "days": days},
        )
    # Each proposal's deletion is audited as itself.
    proposals = prune_proposals()
    # The one job that holds the database every day is when SQLite refreshes
    # its planner statistics.
    optimize()
    return {"deleted": deleted, "days": days, "proposals": proposals}


def _clear_sessions(_progress: Any, *, subject: str, principal: Any) -> dict[str, Any]:
    """A session row outlives its expiry until something deletes it."""

    del subject, principal
    before = Session.objects.count()
    call_command("clearsessions")
    return {"deleted": before - Session.objects.count()}


# What a content sync says on a host that names no index.
NO_CONTENT_INDEX = "No content index set."


def _content_index(progress: Any, *, subject: str, principal: Any) -> dict[str, Any]:
    """The site's content index, synced: a host that names none has no
    content source, which is no failure (settings.CONTENT_INDEX_URL)."""

    del subject, principal
    if not settings.CONTENT_INDEX_URL:
        progress(NO_CONTENT_INDEX)
        return {}
    try:
        return sync_content_index()
    except ContentSyncError as exc:
        raise Failed(str(exc)) from exc


def _public_registry(_progress: Any, *, subject: str, principal: Any) -> dict[str, Any]:
    del subject
    return refresh(principal=principal)


def _unit_state(progress: Any, *, subject: str, principal: Any) -> dict[str, Any]:
    """Tell HQ a unit on its machine failed, which has the controller read
    the machine's units now.

    The failed unit is not named and need not be. The reading says which.
    """

    del subject, principal
    event = {
        "provider": "host",
        "event": UNIT_FAILED,
        "delivery_id": progress.job_id,
        "received_at": timezone.now().isoformat(),
    }
    return apply_events({"source": InboundSource.HOST, "events": [event]})


def _due(_progress: Any, *, subject: str, principal: Any) -> dict[str, Any]:
    """Settle what ran out unanswered, then do the host's and the extensions'
    work whose cadence has come round, each as its own job, so each says how
    it ended.

    Settling is here rather than on a page, since a GET has no effect: a job
    whose process stopped is marked lost, and an approval nobody answered in
    time is marked lapsed. A page already shows each as it stands.
    """

    del subject
    lost = runner.reap()
    lapsed = approvals.lapse_expired()
    ended: dict[str, str] = {}
    for work in outbound_work.due(timezone.now()):
        try:
            ended[work.name] = outbound_work.run_now(work.name, principal=principal).state
        except RecordConflict:
            ended[work.name] = "running"
        except ValueError as refused:
            ended[work.name] = f"refused: {refused}"
    return {"due": len(ended), "ended": ended, "lost": lost, "lapsed": lapsed}


DAILY = timedelta(days=1)


def _own(name: str, label: str, run: Any, every: timedelta | None = None) -> OutboundWork:
    return OutboundWork(name, label, "", "", run, every=every, offered=False)


def outbound() -> tuple[OutboundWork, ...]:
    """Work with ``every`` is done on that cadence by ``due``, which the one
    hourly timer asks for. Work without one is asked for by a unit of its own
    (``units.read``, through ``OnFailure=``), or by HQ when it learns something
    is due sooner than the timer would find it."""

    return (
        _own("audit.prune", "Log cleanup", prune_audit, every=DAILY),
        _own("content.sync", "Content sync", _content_index, every=DAILY),
        _own("due", "Scheduled work", _due),
        _own("events.prune", "Event cleanup", prune_events, every=DAILY),
        _own(public_registry.REFRESH, "Registry refresh", _public_registry, every=DAILY),
        _own("sessions.clear", "Session cleanup", _clear_sessions, every=DAILY),
        _own("units.read", "Service check", _unit_state),
    )
