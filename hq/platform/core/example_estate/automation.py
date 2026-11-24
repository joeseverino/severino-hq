"""The estate's automation: the example procedures adopted, weeks of runs
ending each way, and a run working and one waiting now.

Each is carried out through the paths a real one takes. The repository is
synthetic, three ``example.*`` procedures with a default for every input, so a
seed can ask for a run knowing only its id.
"""

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from itertools import count
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from django.contrib.auth import get_user_model
from django.db.models.signals import pre_save
from django.utils import timezone

from hq.domains.control_plane.models import (
    AdoptedCatalog,
    CapabilityRule,
    ManagedResource,
    OperationRequest,
    SourceProposal,
)
from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND, SOURCE_KIND
from hq.platform.application.agent_registry import observe
from hq.platform.application.approvals import APPROVE, is_held
from hq.platform.application.automation_adoption import ADOPT, catch_up
from hq.platform.application.automation_manifest import OperationManifest
from hq.platform.application.automation_runs import capability_name, operation_registry
from hq.platform.application.automation_sources import REFRESH
from hq.platform.application.cadence import AUTOMATION_HEARTBEAT, AUTOMATION_WORK, note_automation
from hq.platform.application.capabilities import run_capability
from hq.platform.application.controller import ControllerReport, claim_next_operation, renew_operation, report_operation
from hq.platform.application.machines import machine_catalog
from hq.platform.application.refusal_status import refused
from hq.platform.application.security import MCP_INTERFACE, Principal, cli_principal, web_principal
from hq.platform.application.tokens import token_principal
from hq.platform.core.audit import stamped
from hq.platform.core.models import UpstreamReading

from . import doors
from .agents import CLAIMS, ruled
from .names import LAPTOP_NAME, MACHINE, back, hexed, share

# The automation worker, as it names itself when it claims.
WORKER = "example-automation-worker"
CLAIMS_RUNS = ((OPERATION_KIND, "run"),)
CLAIMS_SCANS = ((SOURCE_KIND, "scan"),)
# The namespace a run's id is drawn from, with its idempotency key.
_RUNS = uuid5(NAMESPACE_URL, "https://example.com/runs")

CACHE_FLUSH = "example.cache_flush"
DISK_REPORT = "example.disk_report"
PACKAGE_UPGRADE = "example.package_upgrade"
# The commit the example repository's scan reads.
REVISION = hexed("playbooks", "revision", length=40)


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
            # An author's placeholder, which reads as markup.
            "recovery": ["The cache refills as it is read; journalctl -u example-cache@<name>.service says why not."],
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


# The profiles of the worker's logins: the read account's, and each
# privilege a commit of the example repository names.
PROFILES = ("read", "example.cache_admin", "example.package_admin")


def _reaches() -> list[dict[str, str]]:
    """The worker's logins: each machine HQ knows, as each profile."""

    return [{"machine": machine.name, "profile": profile} for machine in machine_catalog() for profile in PROFILES]


def catalog(
    procedures: tuple[dict[str, Any], ...] = PROCEDURES, revision: str = REVISION, plugins: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The example repository as the worker's scan reports it (``SourceCatalog``),
    holding each plugins directory of ``plugins``."""

    return {
        "branch": "main",
        "revision": revision,
        "verification": {"verified": True, "reason": "valid", "committer": "example-maintainer"},
        "procedures": [dict(procedure) for procedure in procedures],
        "files": _files(procedures),
        "unpinned": [{"path": path, "kind": "plugins"} for path in plugins],
        "reaches": _reaches(),
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
# The newest commit: emptying a cache escalates now, the disk role grows a
# procedure that frees space, so adopting it widens what a run may do, and
# the upgrade leaves the repository; reading a mount's use stays as it is.
WIDENED = (
    _moved(_FLUSH, declared={"privilege": "example.cache_admin"}),
    _REPORT,
    _renamed(
        _REPORT,
        "disk_cleanup",
        declared={
            "effect": "infrastructure_change",
            "retry": "never",
            "timeout_seconds": 600,
            "recovery": ["Deleted files are gone; restore any still needed from the machine's last backup."],
            "fixes": ["filesystem-nearly-full"],
        },
        title="Free space on <mount>",
        preconditions=["The mount point exists on the machine, and its old logs and caches may be deleted."],
        verification=["The mount has more free space than before the run."],
        argument_spec={
            "short_description": "Free space on one mount",
            "options": {"mount": {"type": "path", "default": "/", "description": "The mount point to free space on."}},
        },
    ),
)
WIDENED_REVISION = _commit("Escalate the cache flush; add a disk cleanup; retire the upgrade.")
# The commit before it, which HQ can't adopt: it holds a plugins directory,
# its upgrade names a role the repository does not hold, said as the
# worker's scan says it (``hq_runner.ansible.role_spec``), and a new cache
# change declares neither preconditions nor verification.
UNREADY = (
    _FLUSH,
    _REPORT,
    _moved(_UPGRADE, problems=["Role packages_v2 is missing. Add it at roles/packages_v2/ in the repository."]),
    _renamed(_FLUSH, "cache_warm", title="Warm a cache", preconditions=[], verification=[]),
)
UNREADY_REVISION = _commit("Move the upgrade onto a new role; add a cache warm.")
UNREADY_PLUGINS = ("collections",)


def _scanned(
    person: Principal, key: str, found: dict[str, Any] | None, message: str, ago: timedelta | None = None
) -> OperationRequest:
    """A Refresh asked by ``person``, claimed by its worker and reported:
    reading ``found``, or failing with ``message`` when it found nothing.
    Asked ``ago``, with every row it writes dated so, or now when not given,
    so the estate's scans read on Jobs in the order they tell."""

    at = back(ago, key) if ago is not None else None
    # Its worker reached HQ a moment ago, so what is asked of it is taken.
    note_automation()
    with stamped(lambda *_: {"created_at": at} if at is not None else None):
        with doors.page("command", name="automation.source.refresh"):
            queued = _ran(REFRESH, {}, principal=person, key=key)["operation"]
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
    if at is not None:
        OperationRequest.objects.filter(pk=scan.pk).update(created_at=at, updated_at=at)
        _dated(scan.pk, at)
        SourceProposal.objects.filter(operation=scan).update(created_at=at, updated_at=at)
    scan.refresh_from_db()
    return scan


def adopted(person: Principal) -> None:
    """The example repository refreshed by ``person``, scanned by its worker,
    and what it found adopted by ``person``."""

    scan = _scanned(person, "example-refresh", catalog(), "Read the playbooks.", timedelta(days=20))
    with doors.page("command", name="automation.source.adopt"):
        _ran(ADOPT, {"proposal": scan.result["proposal"]}, principal=person, key=f"example-adopt-{scan.pk}")
    # Adopted the day it was read, as the scan was.
    adopted_at = scan.created_at + timedelta(hours=1)
    AdoptedCatalog.objects.filter(proposal_id=scan.result["proposal"]).update(
        created_at=adopted_at, updated_at=adopted_at
    )
    # A seed's transaction has not committed, so this process reads what it adopted now.
    catch_up()


def proposed(person: Principal) -> None:
    """The commits after the one adopted, each refreshed by ``person``: one
    whose procedures HQ cannot adopt, then the newest, which widens what a
    run may do and waits for a person to adopt or reject it; and a scan that
    failed after them."""

    _scanned(
        person,
        "example-refresh-unready",
        catalog(UNREADY, UNREADY_REVISION, UNREADY_PLUGINS),
        "Read the playbooks.",
        timedelta(days=1),
    )
    _scanned(
        person, "example-refresh-widened", catalog(WIDENED, WIDENED_REVISION), "Read the playbooks.", timedelta(hours=3)
    )
    _scanned(person, "example-refresh-failed", None, "HQ couldn't reach your repository's host.", timedelta(minutes=25))


def rescanned(owner) -> None:
    """A Refresh by ``owner`` after the estate's that found the commit adopted
    now, so nothing waits to be adopted."""

    _scanned(web_principal(owner), "example-refresh-again", catalog(), "Read the playbooks.")


def unadoptable(owner) -> None:
    """A Refresh by ``owner`` after the estate's that read the commit HQ
    can't adopt again, so it is the one that waits."""

    _scanned(
        web_principal(owner),
        "example-refresh-unadoptable",
        catalog(UNREADY, UNREADY_REVISION, UNREADY_PLUGINS),
        "Read the playbooks.",
    )


def scanning(owner) -> None:
    """A Refresh by ``owner`` after the estate's, which its worker has
    claimed and is reading now."""

    note_automation()
    with doors.page("command", name="automation.source.refresh"):
        _ran(REFRESH, {}, principal=web_principal(owner), key="example-refresh-scanning")
    with doors.controller("claim"):
        claim_next_operation(WORKER, capabilities=CLAIMS_SCANS)


def unattended() -> None:
    """HQ's playbook worker last arrived three hours ago, longer than its
    timer lets pass, and its run's lease ran out then: the run that waits
    reads as not started, and Refresh and each run are refused."""

    then = timezone.now() - timedelta(hours=3)
    UpstreamReading.objects.filter(key=AUTOMATION_HEARTBEAT).update(observed_at=then)
    work = OperationRequest.objects.filter(action__in=AUTOMATION_WORK, claimed_at__gt=then)
    work.update(claimed_at=then, lease_expires_at=then)


# The id the agent's runs are found by, as an investigation's would be.
CORRELATION = "example-investigation-7"
# Where the run that waits acts, and where the read last ran, which a
# person can run it on again: the laptop, since a phone runs no playbook.
WAITING_ON = READ_ON = LAPTOP_NAME
# How long the working run's worker holds it from the moment the estate is
# built: as long as anyone browses it, so the run reads as working throughout.
WORKING_LEASE = timedelta(hours=8)


@dataclass(frozen=True, slots=True)
class Ended:
    """How a past run ended, as its worker reported it."""

    success: bool
    message: str
    events: tuple[tuple[str, str, str], ...] = ()
    # The readings its playbook stated on its machine, by kind (``RunStated``).
    readings: dict[str, list[dict[str, Any]]] | None = None


def _events(host: str, events: tuple[tuple[str, str, str], ...]) -> list[dict[str, Any]]:
    return [{"host": host, "task": task, "outcome": outcome, "message": said} for task, outcome, said in events]


# How the server's past runs ended, as their worker reported it: a playbook
# that states no result is said by its verification, which it just held.
DONE = Ended(
    success=True,
    message="The cache reports no entries.",
    events=(("Read what it holds", "ok", ""), ("Empty it", "changed", "Emptied."), ("Check it is empty", "ok", "")),
)
FAILED = Ended(
    success=False,
    message="A task failed; nothing after it ran.",
    events=(("Read what it holds", "ok", ""), ("Empty it", "failed", "The service did not answer.")),
)
# How the read ended on the machine it last ran on: the result its playbook stated.
REPORTED = Ended(
    success=True,
    message="/: 120.5 GB of 460.4 GB used (27%)",
    events=(("Read the mount's use", "ok", ""),),
    readings={
        "host.filesystem": [
            {"mount": "/", "free_bytes": 365226196992, "total_bytes": 494345830400, "read_at": "2026-11-13T12:00:00Z"}
        ]
    },
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
    read_on: str = READ_ON,
    waiting_on: str = WAITING_ON,
    past: int | None = None,
) -> None:
    """The example procedures adopted, then past runs of the first on
    ``server``, a finished run of the second on ``read_on``, and one run
    working and one waiting now.

    Every other machine and the second procedure, a read, stay free to run.
    """

    owner = owner or get_user_model().objects.order_by("pk").first()
    person = web_principal(owner) if owner is not None else cli_principal()
    agent = token_principal(CLAIMS, MCP_INTERFACE)
    # Only a person approves an agent's held ask; the shell may not.
    approver = person if owner is not None else None
    adopted(person)
    proposed(person)
    supplied = procedures()
    first, *others = supplied
    endings: tuple[tuple[Principal, Callable[[OperationRequest], None], str], ...] = (
        (person, _reported(DONE), ""),
        (person, _reported(FAILED), ""),
        (agent, _reported(DONE), ""),
        (agent, _reported(DONE), CORRELATION),
        (person, _called_off(person), ""),
        (person, _outlived, ""),
    )
    count = len(endings) if past is None else past
    for index in range(count):
        asker, carried_out, correlation = endings[index % len(endings)]
        ago = timedelta(days=2 + 3 * (count - 1 - index))
        _asked(first, server, asker, ago, carried_out, approver=approver, correlation=correlation)
    if others:
        _asked(others[0], read_on, person, timedelta(hours=5), _reported(REPORTED), approver=approver)
    # Now: one run working on the server, one waiting.
    _asked(first, server, person, timedelta(minutes=3), _working, approver=approver)
    if len(others) > 1:
        _asked(others[1], waiting_on, agent, timedelta(minutes=1), lambda _run: None, approver=approver)
    changes = [manifest for manifest in supplied if manifest.effect.changes]
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
    # An agent has connected before anyone can set its rules.
    with doors.mcp():
        observe(agent)
    ruled(owner, name, CapabilityRule.Rule.APPROVE)
    key = f"example-held-run-{manifest.id}"
    with doors.mcp():
        answer = run_capability(name, {"hosts": [host]}, principal=agent, idempotency_key=key).answer
    if not is_held(answer):
        raise RuntimeError(f"The agent's run was not held for a person: {answer}")


def _asked(manifest, host, principal, ago, carried_out, *, approver: Principal | None, correlation="") -> None:
    """One run of ``manifest`` on ``host``, asked for ``ago``, approved by
    ``approver`` where the ask waits for a person (left waiting without
    one), then carried out, with every audit row it writes dated to the
    moment it was asked."""

    at = back(ago, manifest.id, host)
    name = capability_name(manifest.id)
    # Each ask through its asker's own door, as the command page or the MCP server binds one.
    door = doors.mcp() if principal.interface == MCP_INTERFACE else doors.request(f"/commands/{name}/")
    key = f"example-run-{manifest.id}-{host}-{int(ago.total_seconds())}"
    with stamped(lambda *_: {"created_at": at}):
        with door:
            answer = _ran(name, {"hosts": [host], "correlation": correlation}, principal=principal, key=key)
        if is_held(answer):
            if approver is None:
                # Nobody here may approve it, so it waits, as a real one would.
                return
            held = answer["approval"]["id"]
            with doors.page("core:approval_entry", approval_id=held):
                answer = _ran(APPROVE, {"approval_id": held}, principal=approver, key=f"{key}-approved")["result"]
        run = OperationRequest.objects.get(pk=answer["operation"]["id"])
        OperationRequest.objects.filter(pk=run.pk).update(created_at=at, updated_at=at)
        carried_out(run)
    _dated(run.pk, at)


def _ran(
    name: str, payload: dict[str, Any], *, principal: Principal, key: str | None, target: str | None = None
) -> dict[str, Any]:
    """What ``principal`` pressing ``name`` answers, through the runner as its page asks it;
    the estate stops where a step it builds on is refused."""

    answer = run_capability(name, payload, principal=principal, target=target, idempotency_key=key).answer
    if refused(answer):
        raise RuntimeError(f"The estate's {name} was refused: {answer}")
    return answer


@contextmanager
def numbered() -> Iterator[None]:
    """Give each operation written in the block an id drawn from its place in
    the order they are written, so every build of the estate gives a run the
    same one and a run's page a look found on one build is found on the server's."""

    written = count()

    def name(sender: type, instance: OperationRequest, **_: Any) -> None:
        if instance._state.adding:
            instance.pk = uuid5(_RUNS, str(next(written)))

    pre_save.connect(name, sender=OperationRequest, weak=False)
    try:
        yield
    finally:
        pre_save.disconnect(name, sender=OperationRequest)


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
            stated={"readings": {run.input["hosts"][0]: ended.readings}} if ended.readings else None,
        )
        with doors.controller("report"):
            report_operation(str(run.pk), report, controller_id=WORKER)

    return carry_out


def _called_off(person: Principal) -> Callable[[OperationRequest], None]:
    """Switched off before its worker took it, which cancels what waits, then on again."""

    def carry_out(run: OperationRequest) -> None:
        resource = ManagedResource.objects.get(key=run.resource.key)
        for enabled in (False, True):
            with doors.page("control_plane:detail", key=resource.key):
                _ran(
                    "infrastructure.resource.update",
                    {"enabled": enabled},
                    principal=person,
                    target=resource.key,
                    key=f"example-switch-{run.pk}-{enabled}",
                )

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
    OperationRequest.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() + WORKING_LEASE)


def _lapse(run: OperationRequest) -> None:
    OperationRequest.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=10))


def _dated(pk, at) -> None:
    """The run's own times, as long after its ask as a worker would take."""

    run = OperationRequest.objects.get(pk=pk)
    claimed = at + timedelta(seconds=5) + share(timedelta(seconds=40), pk, "claimed") if run.claimed_at else None
    dates: dict[str, Any] = {"claimed_at": claimed}
    if run.completed_at:
        dates["completed_at"] = (claimed or at) + timedelta(seconds=30) + share(timedelta(minutes=4), pk, "took")
    OperationRequest.objects.filter(pk=pk).update(updated_at=dates.get("completed_at") or claimed or at, **dates)
