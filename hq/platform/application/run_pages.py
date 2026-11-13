"""What a run's own page shows (``jobs/run_detail.html``): the facts of one
run of a playbook or scan of its source, the approval it ran under, and each
task's outcome in the table every registry-driven band is drawn as."""

from typing import Any

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest

from .action_links import command_url
from .capabilities import capability_registry
from .entity_links import entity_link
from .pages import offered
from .routes import reverse
from .service_context import Cell, ServiceSection
from .work_runs import WorkRun


def approval_of(operation: OperationRequest) -> ApprovalRequest | None:
    """The held request whose approval queued ``operation``, if one did: the
    request whose replayed call answered this operation."""

    if not (operation.input or {}).get("approved_by"):
        return None
    return (
        ApprovalRequest.objects.filter(state=ApprovalRequest.State.APPROVED, result__operation__id=str(operation.pk))
        .order_by("-decided_at")
        .first()
    )


def tasks_section(run: WorkRun) -> ServiceSection | None:
    """Each task's outcome on each machine, as the worker reported it; None before any."""

    records = tuple(
        (
            Cell(str(event.get("task", ""))),
            Cell.of(entity_link("machine", str(event["host"]))) if event.get("host") else Cell(""),
            Cell(str(event.get("outcome", "")), tone=event.get("tone", "")),
            Cell(str(event.get("message", ""))),
        )
        for event in run.evidence.events
    )
    if not records:
        return None
    return ServiceSection(id="tasks", label="Tasks", columns=("Task", "Machine", "Outcome", "Message"), records=records)


def run_context(run: WorkRun, operation: OperationRequest, *, user: Any) -> dict[str, Any]:
    """The run page's context for ``user``, who may open Jobs: the run, the
    page of the command it is run through, the approval it ran under where
    they may read the audit log, and its tasks."""

    approval = approval_of(operation)
    approval_url = reverse("core:approval_entry", kwargs={"approval_id": approval.pk}) if approval else ""
    tasks = tasks_section(run)
    return {
        "run": run,
        "operation": operation,
        # A scan runs no playbook: what it ran is the command that asked for it.
        "ran_label": "Command" if operation.action == OperationRequest.Action.SCAN else "Playbook",
        # A playbook no longer adopted has no command to link to.
        "playbook_url": command_url(run.kind) if capability_registry().get(run.kind) else "",
        "approval_url": approval_url if approval_url and offered(user, approval_url) else "",
        "sections": (tasks,) if tasks else (),
    }
