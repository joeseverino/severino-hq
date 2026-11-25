"""What a run's own page shows beside its record (``jobs/run_detail.html``):
the command it is run through, the chain of the audit log it ran in, what it
shows for itself, and how long it has run."""

from typing import Any

from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.domains.jobs.models import JobState

from .action_links import command_url
from .automation_findings import worker_problems
from .automation_runs import operation_registry
from .capabilities import capability_spec
from .doors import NOBODY
from .history import EVENTS, HistoryGroup, asked_by, chain_made, chain_url
from .moments import moment
from .pages import offered
from .playbook_about import reading_words
from .routes import reverse
from .run_reads import evidence_of
from .security import web_principal
from .work_runs import NOT_STARTED


def run_context(record: dict[str, Any], *, user: Any) -> dict[str, Any]:
    """The run page's context for ``user``, who may open Jobs, beside the run's
    record: the page of the command it is run through, the chain it ran in
    (the press, approval and merge that set it going) where they may read the
    audit log, and, while it has not started because no worker reports, the
    problem that says how to set one up."""

    capability = record["capability"]
    operation = record["operation_id"]
    steps = chain_made(OperationRequest, operation) if offered(user, reverse("core:audit_list")) else ()
    return {
        # Who asked, its approval opening the approval's entry: read as the record's ``by`` was.
        "asked_by": asked_by(OperationRequest, (OperationRequest(pk=operation),)).get(str(operation), NOBODY),
        "events": (HistoryGroup(steps),) if steps else (),
        "events_heading": EVENTS,
        "events_url": chain_url(steps[0].events[0].trace) if steps else "",
        # A scan runs no playbook: what it ran is the command that asked for it.
        "ran_label": "Playbook" if record["playbook"] else "Command",
        # A playbook no longer adopted has no command to link to.
        "playbook_url": command_url(capability) if capability_spec(capability) else "",
        "problems": worker_problems(web_principal(user)) if record["state_label"] == NOT_STARTED else (),
        "evidence": evidence_of(record),
        "reading": _declared_reading(record),
        "duration": _duration(record),
    }


def _declared_reading(record: dict[str, Any]) -> dict[str, Any] | None:
    """Each reading the run's playbook, as adopted now, declares it states
    that the run has not stated (yet), in words, and whether the run ended
    without it. None once each is stated, where the run's readings show it,
    and for a playbook that declares none or is no longer adopted."""

    manifest = operation_registry().get(record["playbook"]) if record["playbook"] else None
    stated = {str(each.get("kind")) for each in record["stated"]}
    missing = [kind for kind in (manifest.readings if manifest else ()) if kind not in stated]
    if not missing:
        return None
    return {"words": "; ".join(map(reading_words, missing)), "ended": bool(record["finished_at"])}


def _duration(record: dict[str, Any]) -> float | None:
    """How long the run worked, or has worked so far while it runs; None before it starts."""

    if record["duration_seconds"] is not None:
        return float(record["duration_seconds"])
    started = moment(record["started_at"])
    if started is None or record["state"] != JobState.RUNNING:
        return None
    return max(0.0, (timezone.now() - started).total_seconds())
