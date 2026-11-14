"""What a run's own page shows (``jobs/run_detail.html``): the facts of one
run of a playbook or scan of its source, and the approval it ran under."""

from typing import Any

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest

from .action_links import command_url
from .automation_findings import worker_problems
from .capabilities import capability_registry
from .pages import offered
from .routes import reverse
from .security import web_principal
from .work_runs import NOT_STARTED, WorkRun


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


def run_context(run: WorkRun, operation: OperationRequest, *, user: Any) -> dict[str, Any]:
    """The run page's context for ``user``, who may open Jobs: the run, the
    page of the command it is run through, the approval it ran under where
    they may read the audit log, and, while it has not started
    because no worker reports, the problem that says how to set one up."""

    approval = approval_of(operation)
    approval_url = reverse("core:approval_entry", kwargs={"approval_id": approval.pk}) if approval else ""
    return {
        "run": run,
        "operation": operation,
        # A scan runs no playbook: what it ran is the command that asked for it.
        "ran_label": "Command" if operation.action == OperationRequest.Action.SCAN else "Playbook",
        # A playbook no longer adopted has no command to link to.
        "playbook_url": command_url(run.kind) if capability_registry().get(run.kind) else "",
        "approval_url": approval_url if approval_url and offered(user, approval_url) else "",
        "problems": worker_problems(web_principal(user)) if run.word == NOT_STARTED else (),
    }
