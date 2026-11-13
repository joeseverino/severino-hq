"""The example estate settled for a first-time visitor, as the demo's healthy
first glance promises; what it does and why is in ``demo/README.md``."""

from hq.domains.control_plane.bridge_actions import CONTROLLER
from hq.domains.control_plane.models import ApprovalRequest, OperationRequest, ProviderInventory
from hq.domains.control_plane.observations.host import EDGE_REBOOT_KIND, FILESYSTEM_KIND, REBOOT_KIND, UNIT_KIND
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.platform.application.action_items import SET_ASIDE
from hq.platform.application.approvals import REJECT
from hq.platform.application.attention import readable_by
from hq.platform.application.automation_runs import capability_name, command_type, request_run
from hq.platform.application.capabilities import run_capability
from hq.platform.application.controller import ControllerReport, claim_next_operation, report_operation
from hq.platform.application.dashboard import work_queue
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD
from hq.platform.application.projection import projection_scope
from hq.platform.application.receipts import UPDATE as RECEIPT_UPDATE, serialize_receipt
from hq.platform.application.refused_reports import READINGS_ACTION
from hq.platform.application.security import internal_principal, web_principal
from hq.platform.application.sweep import record_sweep
from hq.platform.core.example_estate import automation, doors
from hq.platform.core.example_estate.names import stamp
from hq.platform.core.example_estate.sweep import CONTROLLER as CONTROLLER_ID

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
    _swept_since()
    _file_dropped_receipts(person)
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


def _swept_since() -> None:
    """The controller's next sweep, after the machine restarted for its
    updates, a build's leftovers were cleared from its root and the service
    systemd kept restarting was fixed, which leaves it off the reading."""

    now = stamp()
    restarted = {"pending": False, "since": "", "packages": [], "scheduled_action": "", "scheduled_at": ""}
    healthy = {
        REBOOT_KIND: lambda record: {**record, **restarted},
        EDGE_REBOOT_KIND: lambda record: {**record, **restarted},
        FILESYSTEM_KIND: lambda record: {**record, "free_bytes": max(record["free_bytes"], record["total_bytes"] // 2)},
        UNIT_KIND: lambda record: None if record.get("foreign") else record,
    }
    payload = {}
    for kind, settled in healthy.items():
        records = ProviderInventory.objects.filter(kind=kind).values_list("records", flat=True).first() or []
        kept = (settled(record) for record in records)
        payload[kind] = {"ok": True, "records": [{**record, "read_at": now} for record in kept if record]}
    with doors.controller(READINGS_ACTION):
        record_sweep(payload, principal=internal_principal(CONTROLLER), controller_id=CONTROLLER_ID)


def _file_dropped_receipts(person) -> None:
    """Each receipt dropped in and not yet filed, linked to the expense of its
    amount and vendor without a receipt, as its match page links one."""

    for receipt in Receipt.objects.filter(related_expense__isnull=True, related_asset__isnull=True):
        expense = Expense.objects.filter(
            total_cost=receipt.amount, vendor=receipt.vendor, receipts__isnull=True
        ).first()
        if expense is None:
            continue
        details = serialize_receipt(receipt)
        fields = ("vendor", "date", "amount", "notes", "related_asset")
        with doors.page("receipts:match", pk=receipt.pk):
            answer = run_capability(
                RECEIPT_UPDATE,
                {field: details[field] for field in fields} | {"related_expense": expense.pk},
                principal=person,
                target=receipt.pk,
                idempotency_key=f"demo-file-receipt-{receipt.pk}",
            ).answer
        _refused(answer)


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
