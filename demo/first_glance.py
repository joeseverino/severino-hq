"""The example estate settled for a first-time visitor, as the demo's healthy
first glance promises; what it does and why is in ``demo/README.md``."""

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.action_items import SET_ASIDE
from hq.platform.application.approvals import REJECT
from hq.platform.application.attention import readable_by
from hq.platform.application.automation_runs import capability_name, command_type, request_run
from hq.platform.application.capabilities import run_capability
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.dashboard import work_queue
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD
from hq.platform.application.projection import projection_scope
from hq.platform.application.security import web_principal
from hq.platform.core.example_estate import automation, doors

# The sets that are the example estate; the empty one has nothing to settle.
ESTATES = ("showcase", "standard")
# The agent's request kept waiting for a person: its change to a DNS rewrite.
_KEPT_REQUEST = "infrastructure.resource.update"
# The rest of what is left to do on purpose, by the start of its queue key:
# an image with an update, an image run by tag alone, a certificate to renew.
_KEPT = (
    "hq.containers:container-updates",
    "hq.infrastructure:finding:container-image-unpinned:",
    "hq.estate:estate-certificate:",
)


def settle(owner) -> list[str]:
    """The estate as its owner keeps it, and the keys of what is left to do on purpose."""

    person = web_principal(owner)
    _decide_held(person)
    # A later Refresh read the commit adopted, so no scan stands failed or unadoptable.
    automation.rescanned(owner)
    _finish_waiting()
    _run_again_what_did_not_finish(person)
    return _set_aside_the_rest(person)


def _decide_held(person) -> None:
    for held in ApprovalRequest.objects.filter(state=ApprovalRequest.State.PENDING).exclude(capability=_KEPT_REQUEST):
        with doors.page("control_plane:approval_decision", approval_id=held.id):
            answer = run_capability(
                REJECT,
                {"approval_id": str(held.id), "note": "Not now."},
                principal=person,
                idempotency_key=f"demo-reject-{held.id}",
            ).answer
        _refused(answer)


def _refused(answer: dict) -> None:
    """A refusal stops the build: a demo settled halfway would break its promise in silence."""

    if not answer.get("ok"):
        raise RuntimeError(f"The demo's settling was refused: {answer}")


def _report(operation_id: str) -> None:
    """The worker's report that the run did what it was asked on each machine."""

    run = OperationRequest.objects.get(pk=operation_id)
    report = ControllerReport(
        success=True,
        observed_generation=run.input["generation"],
        message="Done on every machine.",
        events=[
            {"host": host, "task": "Run the playbook", "outcome": "ok", "message": ""} for host in run.input["hosts"]
        ],
    )
    with doors.controller("report"):
        report_operation(operation_id, report, controller_id=automation.WORKER)


def _finish_waiting() -> None:
    """The worker takes each run that waits for it and finishes it; a claim
    also ends a run whose worker stopped reporting."""

    while True:
        with doors.controller("claim"):
            claimed = claim_next_operation(automation.WORKER, capabilities=automation.CLAIMS_RUNS)["operation"]
        if claimed is None:
            return
        _report(claimed["id"])


def _run_again_what_did_not_finish(person) -> None:
    """Each playbook whose newest run ended without finishing, run again on the
    same machines, and finished."""

    ended = (OperationRequest.State.FAILED, OperationRequest.State.CANCELLED)
    for manifest in automation.procedures():
        newest = (
            OperationRequest.objects.filter(action=OperationRequest.Action.RUN, input__operation=manifest.id)
            .order_by("-created_at")
            .first()
        )
        if newest is None or newest.state not in ended:
            continue
        key = f"demo-run-again-{manifest.id}"
        command = command_type(manifest).model_validate(
            {"hosts": newest.input["hosts"], **({IDEMPOTENCY_FIELD: key} if manifest.effect != "read" else {})}
        )
        with doors.request(f"/commands/{capability_name(manifest.id)}/"):
            request_run(manifest, command, principal=person, idempotency_key=key)
    _finish_waiting()


def _set_aside_the_rest(person) -> list[str]:
    kept_request = ApprovalRequest.objects.filter(state=ApprovalRequest.State.PENDING, capability=_KEPT_REQUEST)
    kept_ids = {str(pk) for pk in kept_request.values_list("pk", flat=True)}
    with projection_scope():
        keys = [item["key"] for item in readable_by(person, work_queue())]
    kept = [key for key in keys if key.startswith(_KEPT) or key.rpartition(":")[2] in kept_ids]
    rest = [key for key in keys if key not in kept]
    with doors.page("action_items_set_aside"):
        _refused(run_capability(SET_ASIDE, {"keys": rest}, principal=person, idempotency_key="demo-set-aside").answer)
    return kept
