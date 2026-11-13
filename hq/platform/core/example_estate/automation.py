"""The estate's automation: the example repository's procedures adopted, then
weeks of one procedure's runs on the server and a run in each state a run can
be in now, each asked for and carried out through the paths a real one takes
(a Refresh, the worker's scan, Adopt; then ``automation_runs.request_run``,
the worker's claim, renewal and report, or its lease running out).

The repository is synthetic, as a scan of it would report it: three
``example.*`` procedures, each with a default for every input, so a seed can
ask for a run of one it knows only by its id. A procedure has one active run,
and a machine one active run, at a time, so the run that waits and the run
whose worker stopped are each another procedure's on another machine.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from hq.domains.control_plane.models import CapabilityRule, ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND, SOURCE_KIND
from hq.platform.application.approvals import is_held
from hq.platform.application.automation_adoption import ADOPT, AdoptProposal, adopt, catch_up
from hq.platform.application.automation_manifest import OperationManifest
from hq.platform.application.automation_runs import capability_name, command_type, operation_registry, request_run
from hq.platform.application.automation_sources import RefreshCommand, refresh
from hq.platform.application.capabilities import run_capability
from hq.platform.application.controller import ControllerReport, claim_next_operation, renew_operation, report_operation
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD
from hq.platform.application.security import MCP_INTERFACE, Principal, cli_principal, web_principal
from hq.platform.application.tokens import token_principal
from hq.platform.core.audit import stamped

from . import doors
from .agents import AGENT, CLAIMS
from .names import LAPTOP_NAME, MACHINE, PHONE_NAME

# The automation worker, as it names itself when it claims.
WORKER = "example-automation-worker"
CLAIMS_RUNS = ((OPERATION_KIND, "run"),)
CLAIMS_SCANS = ((SOURCE_KIND, "scan"),)

CACHE_FLUSH = "example.cache_flush"
DISK_REPORT = "example.disk_report"
PACKAGE_UPGRADE = "example.package_upgrade"
# The commit the example repository's scan reads.
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _procedure(name: str, role: str, declaration: dict[str, Any], **found: Any) -> dict[str, Any]:
    """One procedure as the worker's scan reports it (``ScannedProcedure``)."""

    return {
        "sidecar": f"playbooks/{name}.hq.yml",
        "playbook": f"playbooks/{name}.yml",
        "declaration": {"target_kinds": ["machine"], **declaration},
        "roles": [{"role": role, "argument_spec": found.pop("argument_spec")}],
        "problems": [],
        **found,
    }


PROCEDURES = (
    _procedure(
        "cache_flush",
        "cache",
        {
            "id": CACHE_FLUSH,
            "effect": "infrastructure_change",
            "timeout_seconds": 300,
            "retry": "observe_first",
            "recovery": ["The cache refills on its own as it is read."],
        },
        title="Empty a cache",
        preconditions=["The cache's service is running on the machine."],
        verification=["The cache reports no entries."],
        argument_spec={
            "short_description": "Empty one cache",
            "options": {
                "cache": {
                    "type": "str",
                    "choices": ["web", "api"],
                    "default": "web",
                    "description": "The cache to empty.",
                }
            },
        },
    ),
    _procedure(
        "disk_report",
        "disk",
        {
            "id": DISK_REPORT,
            "effect": "read",
            "timeout_seconds": 60,
            "retry": "safe",
            "recovery": ["Nothing changes, so nothing needs recovering."],
            "inputs": {"mount": {"pattern": "^/[A-Za-z0-9/_.-]*$", "max_length": 200}},
        },
        title="Report a mount's use",
        preconditions=["The mount point exists on the machine."],
        verification=["The report names the mount's size and use."],
        argument_spec={
            "short_description": "Report one mount",
            "options": {"mount": {"type": "path", "default": "/", "description": "The mount point to report on."}},
        },
    ),
    _procedure(
        "package_upgrade",
        "packages",
        {
            "id": PACKAGE_UPGRADE,
            "effect": "infrastructure_change",
            "privilege": "example.package_admin",
            "timeout_seconds": 1800,
            "retry": "never",
            "recovery": ["Pin the package back to its last version and restart what uses it."],
        },
        title="Upgrade packages",
        preconditions=["The machine's package index is reachable."],
        verification=["No upgradable package asked for is left."],
        argument_spec={
            "short_description": "Upgrade the machine's packages",
            "options": {
                "security_only": {
                    "type": "bool",
                    "default": True,
                    "description": "Whether to apply only security updates.",
                }
            },
        },
    ),
)


def _files(procedures: tuple[dict[str, Any], ...] = PROCEDURES) -> list[dict[str, str]]:
    paths = sorted(
        {
            *(path for procedure in procedures for path in (procedure["sidecar"], procedure["playbook"])),
            *(f"roles/{role['role']}/tasks/main.yml" for procedure in procedures for role in procedure["roles"]),
            *(
                f"roles/{role['role']}/meta/argument_specs.yml"
                for procedure in procedures
                for role in procedure["roles"]
            ),
            "README.md",
        }
    )
    return [{"path": path, "object": hashlib.sha1(path.encode(), usedforsecurity=False).hexdigest()} for path in paths]


def catalog(procedures: tuple[dict[str, Any], ...] = PROCEDURES, revision: str = REVISION) -> dict[str, Any]:
    """The example repository as the worker's scan reports it (``SourceCatalog``)."""

    return {
        "branch": "main",
        "revision": revision,
        "verification": {"verified": True, "reason": "valid", "committer": "example-maintainer"},
        "procedures": [dict(procedure) for procedure in procedures],
        "files": _files(procedures),
        "unpinned": [],
    }


def _commit(message: str) -> str:
    """A later commit of the example repository, named by what it changed."""

    return hashlib.sha1(message.encode(), usedforsecurity=False).hexdigest()


def _moved(procedure: dict[str, Any], *, declared: dict[str, Any] | None = None, **found: Any) -> dict[str, Any]:
    """``procedure`` as a later commit declares it: its declaration's terms in
    ``declared`` replaced, and what the scan found in ``found``."""

    return {**procedure, "declaration": {**procedure["declaration"], **(declared or {})}, **found}


def _renamed(procedure: dict[str, Any], name: str, **found: Any) -> dict[str, Any]:
    """A new procedure the repository grew from ``procedure``: its files and id named ``name``."""

    declared = {"id": f"example.{name}", **found.pop("declared", {})}
    return _moved(
        procedure, declared=declared, sidecar=f"playbooks/{name}.hq.yml", playbook=f"playbooks/{name}.yml", **found
    )


_FLUSH, _REPORT, _UPGRADE = PROCEDURES
# The next commit: emptying a cache escalates now, and the disk role grows a
# procedure that frees space, so adopting it widens what a run may do.
WIDENED = (
    _moved(_FLUSH, declared={"privilege": "example.cache_admin"}),
    _REPORT,
    _renamed(
        _REPORT,
        "disk_cleanup",
        declared={"effect": "infrastructure_change", "retry": "never"},
        title="Free a mount's space",
    ),
    _UPGRADE,
)
WIDENED_REVISION = _commit("Escalate the cache flush; add a disk cleanup.")
# The commit after it, whose upgrade names a role the repository does not hold.
UNREADY = (_FLUSH, _REPORT, _moved(_UPGRADE, problems=["The playbook names a role the repository does not hold."]))
UNREADY_REVISION = _commit("Move the upgrade onto a new role.")


def _scanned(person: Principal, key: str, found: dict[str, Any] | None, message: str) -> OperationRequest:
    """A Refresh asked by ``person``, claimed by its worker and reported:
    reading ``found``, or failing with ``message`` when it found nothing."""

    with doors.page("command", name="automation.source.refresh"):
        queued = refresh(RefreshCommand(idempotency_key=key), principal=person)["operation"]
    with doors.controller("claim"):
        claimed = claim_next_operation(WORKER, capabilities=CLAIMS_SCANS)["operation"]
    if claimed is None or claimed["id"] != queued["id"]:
        raise RuntimeError("The estate's scan was not the one its worker claimed.")
    scan = OperationRequest.objects.get(pk=queued["id"])
    report = ControllerReport(
        success=found is not None, observed_generation=scan.input["generation"], message=message, catalog=found
    )
    with doors.controller("report"):
        report_operation(queued["id"], report, controller_id=WORKER)
    scan.refresh_from_db()
    return scan


def adopted(person: Principal) -> None:
    """The example repository refreshed by ``person``, scanned by its worker,
    and what it found adopted by ``person``."""

    scan = _scanned(person, "example-refresh", catalog(), "Read the procedures.")
    with doors.page("command", name="automation.source.adopt"):
        adopt(AdoptProposal(proposal=scan.result["proposal"]), principal=person)
    # A seed's transaction has not committed, so this process reads what it adopted now.
    catch_up()


def proposed(person: Principal, agent: Principal) -> None:
    """The commits after the one adopted, each refreshed by ``person``: one
    that widens what a run may do, which ``agent`` asks to adopt and so waits
    for a person; one whose procedures HQ cannot adopt; and a scan that failed."""

    widened = _scanned(person, "example-refresh-widened", catalog(WIDENED, WIDENED_REVISION), "Read the procedures.")
    with doors.mcp():
        held = run_capability(
            ADOPT, {"proposal": widened.result["proposal"]}, principal=agent, idempotency_key="example-agent-adopt"
        ).answer
    if not is_held(held):
        raise RuntimeError(f"The agent's adoption was not held for a person: {held}")
    _scanned(person, "example-refresh-unready", catalog(UNREADY, UNREADY_REVISION), "Read the procedures.")
    _scanned(person, "example-refresh-failed", None, "The repository could not be fetched: the host did not answer.")


def rescanned(owner) -> None:
    """A Refresh by ``owner`` after the estate's that found the commit adopted
    now, so nothing waits to be adopted."""

    _scanned(web_principal(owner), "example-refresh-again", catalog(), "Read the procedures.")


def scanning(owner) -> None:
    """A Refresh by ``owner`` after the estate's, which its worker has
    claimed and is reading now."""

    with doors.page("command", name="automation.source.refresh"):
        refresh(RefreshCommand(idempotency_key="example-refresh-scanning"), principal=web_principal(owner))
    with doors.controller("claim"):
        claim_next_operation(WORKER, capabilities=CLAIMS_SCANS)


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
    """The ``example.*`` procedures adopted, each one a seed can ask for with no inputs, by id."""

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
    """The example procedures adopted, then past runs of the first on
    ``server``, ``past`` of them (one of each ending by default) three days
    apart, then the three a run can be in now."""

    owner = owner or get_user_model().objects.order_by("pk").first()
    person = web_principal(owner) if owner is not None else cli_principal()
    agent = token_principal(CLAIMS, MCP_INTERFACE)
    adopted(person)
    proposed(person, agent)
    supplied = procedures()
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
    changes = [manifest for manifest in supplied if manifest.effect != "read"]
    if changes:
        _held(changes[-1], server, agent, owner)
    # Each procedure's record was written when it was first asked for.
    for record in ManagedResource.objects.filter(kind=OPERATION_KIND):
        first_ask = record.operations.order_by("created_at").values_list("created_at", flat=True).first()
        ManagedResource.objects.filter(pk=record.pk).update(created_at=first_ask)


def _held(manifest, host, agent, owner) -> None:
    """The operator asks to see the agent's runs of ``manifest`` first, so
    the one it asks for on ``host`` waits for a person."""

    name = capability_name(manifest.id)
    CapabilityRule.objects.create(subject=AGENT, capability=name, rule=CapabilityRule.Rule.APPROVE, changed_by=owner)
    key = f"example-held-run-{manifest.id}"
    with doors.mcp():
        answer = run_capability(
            name, {"hosts": [host], IDEMPOTENCY_FIELD: key}, principal=agent, idempotency_key=key
        ).answer
    if not is_held(answer):
        raise RuntimeError(f"The agent's run was not held for a person: {answer}")


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
