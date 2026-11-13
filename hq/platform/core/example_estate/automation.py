"""The estate's automation runs: weeks of one procedure on the server, and a
run in each state a run can be in now, each asked for and carried out through
the paths a real one takes (``automation_runs.request_run``, then the worker's
claim, renewal and report, or its lease running out).

The procedures are whatever ``example.*`` ones the composition supplies (the
example plugin's, ``tests/fixtures/example_hq_plugin/operations.py``), since
HQ names none of its own; with none supplied there is nothing to run, and the
estate has no runs. A procedure has one active run, and a machine one active
run, at a time, so the run that waits and the run whose worker stopped are
each another procedure's on another machine.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND
from hq.platform.application.automation_manifest import OperationManifest
from hq.platform.application.automation_runs import capability_name, command_type, operation_registry, request_run
from hq.platform.application.controller import ControllerReport, claim_next_operation, renew_operation, report_operation
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD
from hq.platform.application.security import MCP_INTERFACE, Principal, cli_principal, web_principal
from hq.platform.application.tokens import token_principal
from hq.platform.core.audit import stamped

from . import doors
from .agents import CLAIMS
from .names import LAPTOP_NAME, MACHINE, PHONE_NAME

# The automation worker, as it names itself when it claims.
WORKER = "example-automation-worker"
CLAIMS_RUNS = ((OPERATION_KIND, "run"),)
# The id the agent's runs are found by, as an investigation's would be.
CORRELATION = "example-investigation-7"
# Where the run that waits and the run whose worker stopped act.
WAITING_ON, STOPPED_ON = LAPTOP_NAME, PHONE_NAME


@dataclass(frozen=True, slots=True)
class Ended:
    """How a past run ended, as its worker reported it."""

    success: bool
    message: str
    events: tuple[tuple[str, str, str], ...] = ()


def _events(host: str, events: tuple[tuple[str, str, str], ...]) -> list[dict[str, Any]]:
    return [{"host": host, "task": task, "outcome": outcome, "message": said} for task, outcome, said in events]


# How the server's past runs ended, as their worker reported it.
DONE = Ended(
    success=True,
    message="Done on every machine.",
    events=(("Read what it holds", "ok", ""), ("Empty it", "changed", "Emptied."), ("Check it is empty", "ok", "")),
)
FAILED = Ended(
    success=False,
    message="A task failed; nothing after it ran.",
    events=(("Read what it holds", "ok", ""), ("Empty it", "failed", "The service did not answer.")),
)


def procedures() -> list[OperationManifest]:
    """The ``example.*`` procedures supplied, each one a seed can ask for with no inputs, by id."""

    return [
        manifest
        for operation_id, manifest in sorted(operation_registry().items())
        if operation_id.split(".")[0] == "example"
        and not any(field.is_required() for field in manifest.input_type.model_fields.values())
    ]


def runs(
    owner=None,
    *,
    server: str = MACHINE,
    stopped_on: str = STOPPED_ON,
    waiting_on: str = WAITING_ON,
    past: int | None = None,
) -> None:
    """Past runs of the first procedure on ``server``, ``past`` of them (one of
    each ending by default) three days apart, then the three a run can be in now."""

    supplied = procedures()
    if not supplied:
        return
    owner = owner or get_user_model().objects.order_by("pk").first()
    person = web_principal(owner) if owner is not None else cli_principal()
    agent = token_principal(CLAIMS, MCP_INTERFACE)
    first, *others = supplied
    approved = replace(agent, approved_by=person.actor)
    endings: tuple[tuple[Principal, Callable[[OperationRequest], None], str], ...] = (
        (person, _reported(DONE), ""),
        (person, _reported(FAILED), ""),
        (approved, _reported(DONE), ""),
        (agent, _reported(DONE), CORRELATION),
        (person, _called_off(person), ""),
        (person, _outlived, ""),
    )
    count = len(endings) if past is None else past
    for index in range(count):
        asker, carried_out, correlation = endings[index % len(endings)]
        ago = timedelta(days=2 + 3 * (count - 1 - index))
        _asked(first, server, asker, ago, carried_out, correlation=correlation)
    # Now: one run working on the server, one whose worker went quiet, one waiting.
    _asked(first, server, person, timedelta(minutes=3), _working)
    if others:
        _asked(others[0], stopped_on, person, timedelta(minutes=40), _went_quiet)
    if len(others) > 1:
        _asked(others[1], waiting_on, agent, timedelta(minutes=1), lambda _run: None)
    # Each procedure's record was written when it was first asked for.
    for record in ManagedResource.objects.filter(kind=OPERATION_KIND):
        first_ask = record.operations.order_by("created_at").values_list("created_at", flat=True).first()
        ManagedResource.objects.filter(pk=record.pk).update(created_at=first_ask)


def _asked(manifest, host, principal, ago, carried_out, *, correlation="") -> None:
    """One run of ``manifest`` on ``host``, asked for ``ago``, then carried out,
    with every audit row it writes dated to the moment it was asked."""

    at = timezone.now() - ago
    key = f"example-run-{manifest.id}-{int(ago.total_seconds())}"
    command = command_type(manifest).model_validate(
        {"hosts": [host], "correlation": correlation, **({IDEMPOTENCY_FIELD: key} if manifest.effect != "read" else {})}
    )
    # Each ask through its asker's own door, as the command page or the MCP server binds one.
    door = (
        doors.mcp()
        if principal.interface == MCP_INTERFACE
        else doors.request(f"/commands/{capability_name(manifest.id)}/")
    )
    with stamped(lambda *_: {"created_at": at}):
        with door:
            queued = request_run(manifest, command, principal=principal, idempotency_key=key)["operation"]
        run = OperationRequest.objects.get(pk=queued["id"])
        OperationRequest.objects.filter(pk=run.pk).update(created_at=at, updated_at=at)
        carried_out(run)
    _dated(run.pk, at)


def _claimed(run: OperationRequest) -> dict[str, Any]:
    with doors.controller("claim"):
        claimed = claim_next_operation(WORKER, capabilities=CLAIMS_RUNS, attempt=f"claim-{run.pk.hex[:12]}")[
            "operation"
        ]
    if claimed is None or claimed["id"] != str(run.pk):
        raise RuntimeError("The estate's run was not the one its worker claimed.")
    return claimed


def _reported(ended: Ended) -> Callable[[OperationRequest], None]:
    def carry_out(run: OperationRequest) -> None:
        _claimed(run)
        report = ControllerReport(
            success=ended.success,
            observed_generation=run.input["generation"],
            message=ended.message,
            events=_events(run.input["hosts"][0], ended.events),
        )
        with doors.controller("report"):
            report_operation(str(run.pk), report, controller_id=WORKER)

    return carry_out


def _called_off(person: Principal) -> Callable[[OperationRequest], None]:
    """Switched off before its worker took it, which cancels what waits, then on again."""

    def carry_out(run: OperationRequest) -> None:
        resource = ManagedResource.objects.get(key=run.resource.key)
        for enabled in (False, True):
            command = ManagedResourceCommand(key=resource.key, kind=resource.kind, spec=resource.spec, enabled=enabled)
            with doors.page("control_plane:detail", key=resource.key):
                save_managed_resource(command, principal=person, current_key=resource.key)

    return carry_out


def _outlived(run: OperationRequest) -> None:
    """Its worker stopped renewing, and the next claim ended it: whether it ran is not known."""

    _claimed(run)
    _lapse(run)
    with doors.controller("claim"):
        claim_next_operation(WORKER, capabilities=CLAIMS_RUNS)


def _working(run: OperationRequest) -> None:
    _claimed(run)
    with doors.controller("renew"):
        renew_operation(str(run.pk), controller_id=WORKER)


def _went_quiet(run: OperationRequest) -> None:
    """Claimed, and its lease ran out with no claim since to end it."""

    _claimed(run)
    _lapse(run)


def _lapse(run: OperationRequest) -> None:
    OperationRequest.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=10))


def _dated(pk, at) -> None:
    """The run's own times, as long after its ask as a worker would take."""

    run = OperationRequest.objects.get(pk=pk)
    claimed = at + timedelta(seconds=20) if run.claimed_at else None
    dates: dict[str, Any] = {"claimed_at": claimed}
    if run.completed_at:
        dates["completed_at"] = (claimed or at) + timedelta(seconds=95)
    if run.state == OperationRequest.State.CLAIMED and run.lease_expires_at and run.lease_expires_at < timezone.now():
        # A worker that went quiet was last heard from a few minutes after it began.
        dates["lease_expires_at"] = claimed + timedelta(minutes=5)
    OperationRequest.objects.filter(pk=pk).update(updated_at=dates.get("completed_at") or claimed or at, **dates)
