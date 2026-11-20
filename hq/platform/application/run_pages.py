"""What a run's own page shows beside its record (``jobs/run_detail.html``):
the command it is run through, and the approval it ran under."""

from typing import Any

from hq.domains.control_plane.models import ApprovalRequest

from .action_links import command_url
from .automation_findings import worker_problems
from .capabilities import capability_spec
from .pages import offered
from .routes import reverse
from .security import web_principal
from .work_runs import NOT_STARTED


def approval_of(record: dict[str, Any]) -> ApprovalRequest | None:
    """The held request whose approval queued the run ``record`` is, if one
    did: the request whose replayed call answered it."""

    if not record["approved_by"]:
        return None
    return (
        ApprovalRequest.objects.filter(
            state=ApprovalRequest.State.APPROVED, result__operation__id=record["operation_id"]
        )
        .order_by("-decided_at")
        .first()
    )


def run_context(record: dict[str, Any], *, user: Any) -> dict[str, Any]:
    """The run page's context for ``user``, who may open Jobs, beside the run's
    record: the page of the command it is run through, the approval it ran
    under where they may read the audit log, and, while it has not started
    because no worker reports, the problem that says how to set one up."""

    approval = approval_of(record)
    approval_url = reverse("core:approval_entry", kwargs={"approval_id": approval.pk}) if approval else ""
    capability = record["capability"]
    return {
        # A scan runs no playbook: what it ran is the command that asked for it.
        "ran_label": "Playbook" if record["playbook"] else "Command",
        # A playbook no longer adopted has no command to link to.
        "playbook_url": command_url(capability) if capability_spec(capability) else "",
        "approval_url": approval_url if approval_url and offered(user, approval_url) else "",
        "problems": worker_problems(web_principal(user)) if record["state_label"] == NOT_STARTED else (),
    }
