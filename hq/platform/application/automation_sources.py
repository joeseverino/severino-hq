"""Refresh: asking HQ's runner which playbooks the repository it holds
declares, and storing what it found as a scan result HQ judged.

Nothing is fetched here. A Refresh queues one scan of the automation source
(``automation.source:scan``) and rings the worker's doorbell; the worker reads
the tip of the repository's default branch with the token only it holds and
reports a catalog over the bridge (``SourceCatalog``). HQ holds each sidecar
to ``ProcedureDeclaration``, the model it has one home in, and stores the
catalog with every way a procedure falls short. A proposal is never adopted
and never runnable. How the scan reads a repository: docs/Architecture.md,
"Refreshing the playbooks".
"""

import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any

from django.db import transaction
from django.utils import timezone
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    ValidationError,
)

lazy from hq.domains.control_plane import models
lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND
lazy from hq.platform.core.audit import audit_operation

from .automation_manifest import DottedName, ProcedureTerms
from .command_fields import Describe
from .integration_specs import CapabilitySpec
from .security import Capability, Principal
from .ui import ended
lazy from . import asks, automation_adoption
lazy from .action_links import capability_action_link
lazy from .approval_review import grouped, marked
lazy from .cadence import ring_automation_doorbell
lazy from .findings import rule_for
lazy from .idempotency import IdempotencyConflict
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource
lazy from .moments import ago
lazy from .pages import PageAction
lazy from .record_errors import RecordNotFound
lazy from .resource_operations import OperationQueued, enqueue, serialize_operation
lazy from .routes import reverse
lazy from .work_runs import run_seen_state, run_url

if TYPE_CHECKING:
    from hq.domains.control_plane.models import (
        AdoptedCatalog,
        ManagedResource,
        OperationRequest,
        SourceProposal,
    )

    from .approval_review import ChangePreview
    from .asks import Standing

REFRESH = "automation.source.refresh"
# The page the source's and its playbooks' commands belong to.
PLAYBOOKS = "control_plane:playbooks"
# The one source's record: the repository is the host's configuration, so
# HQ knows of one source and never which repository it is.
SOURCE_KEY = "automation-source"
# As the bridge contract's ``ScannedProcedure`` bounds a procedure's problems.
PROBLEM_LIMIT = 500
MOST_PROBLEMS = 20
# The characters of a commit's id a page names it by: enough to tell two apart.
SHORT_REVISION = 12
# The doorbell starts the worker at once and its container starts in seconds,
# so a scan nobody has taken by then, with the worker busy with nothing else,
# waits on a worker that is not coming.
CLAIM_EXPECTED = timedelta(minutes=2)
# What a queued scan says it waits on, and what to do once the worker has
# not come.
WORKER_DUE = "Waiting for HQ's playbook worker."
WORKER_SEEN = "it last took work {ago}"
WORKER_NEVER = "it has never taken work here"
WORKER_ABSENT = (
    "HQ's playbook worker hasn't taken it; {seen}. On HQ's host, hq-host automation run as root says why. "
    "A development server has no worker."
)


def _pattern(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{value!r} is not a regular expression: {exc}") from None
    return value


class InputBound(BaseModel):
    """What Ansible's argument spec cannot say of a text input: the shape it
    must have and how long it may be."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pattern: Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_pattern)]
    max_length: Annotated[int, Field(ge=1, le=4096)]


class ProcedureDeclaration(ProcedureTerms):
    """A procedure's sidecar (``<name>.hq.yml`` beside ``<name>.yml``): what
    HQ needs to know of it that Ansible has no word for. Its title, its
    preconditions and verification and its inputs' types are Ansible's own,
    read from the playbook and its roles."""

    # A sidecar that names none escalates through none; a manifest says so
    # outright, since an extension's author writes it by hand.
    privilege: DottedName | None = None
    # The bounds of each text input its roles' argument specs declare, by name.
    inputs: dict[Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,63}$")], InputBound] = Field(
        default_factory=dict
    )


def _clip(text: str) -> str:
    return text if len(text) <= PROBLEM_LIMIT else text[: PROBLEM_LIMIT - 1] + "…"


def _declaration_problems(declaration: Any) -> list[str]:
    if declaration is None:
        return []
    try:
        # As JSON, which the sidecar was read into: a list is a strict tuple there.
        ProcedureDeclaration.model_validate_json(json.dumps(declaration))
    except ValidationError as exc:
        return [
            _clip(ended(f"Its sidecar's {'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"))
            for error in exc.errors(include_url=False)
        ]
    return []


def _unknown_fixes(declaration: Any) -> list[str]:
    """A problem for each finding a sidecar says its playbook fixes that HQ raises none of."""

    fixes = declaration.get("fixes") if isinstance(declaration, dict) else None
    if not isinstance(fixes, list):
        return []
    return [
        f"Its sidecar's fixes names {rule}, which is no finding HQ raises."
        for rule in fixes
        if isinstance(rule, str) and rule_for(rule) is None
    ]


def judged(procedures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each procedure as the worker reported it, with every way HQ finds it
    falls short of a procedure it could adopt added to its problems."""

    ids = Counter(
        procedure["declaration"].get("id") for procedure in procedures if isinstance(procedure.get("declaration"), dict)
    )
    found = []
    for procedure in procedures:
        problems = [*procedure["problems"], *_declaration_problems(procedure["declaration"])]
        declaration = procedure["declaration"]
        named = declaration.get("id") if isinstance(declaration, dict) else None
        if isinstance(named, str) and ids[named] > 1:
            problems.append(f"Another sidecar names the id {named} too.")
        problems += _unknown_fixes(declaration)
        found.append({**procedure, "problems": problems[:MOST_PROBLEMS]})
    return found


def propose(operation: OperationRequest, catalog: dict[str, Any]) -> SourceProposal:
    """Store what one scan found, held to the bridge contract at its door."""

    verification = catalog["verification"]
    return models.SourceProposal.objects.create(
        operation=operation,
        branch=catalog["branch"],
        revision=catalog["revision"],
        verified=verification["verified"],
        verification_reason=verification["reason"],
        committer=verification["committer"],
        procedures=judged(catalog["procedures"]),
        files=catalog["files"],
        unpinned=catalog["unpinned"],
        reaches=catalog["reaches"],
    )


class RefreshCommand(BaseModel):
    """A Refresh takes nothing but why it was asked and its retry key: the
    repository is the host's configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Annotated[StrictStr, Field(max_length=300), Describe("Why it is refreshed, kept with the scan.")] = ""
    # Filled by the capability runner with the caller's key, or a fresh one.
    idempotency_key: StrictStr = ""


def _source(principal: Principal) -> ManagedResource:
    current = models.ManagedResource.objects.select_for_update().filter(key=SOURCE_KEY).first()
    if current is None:
        save_managed_resource(
            ManagedResourceCommand(key=SOURCE_KEY, kind=SOURCE_KIND, spec={"named_by": "host"}), principal=principal
        )
    return models.ManagedResource.objects.select_for_update().get(key=SOURCE_KEY)


@transaction.atomic
def refresh(command: RefreshCommand, *, principal: Principal) -> OperationQueued:
    """Queue one scan of the automation source for the worker, once per retry
    key, and answer the one already waiting while there is one."""

    key = command.idempotency_key or f"refresh:{secrets.token_urlsafe(18)}"
    existing = models.OperationRequest.objects.filter(idempotency_key=key).select_related("resource").first()
    if existing is not None:
        if existing.action != models.OperationRequest.Action.SCAN:
            raise IdempotencyConflict("That retry key was already used for a different request.")
        return {"ok": True, "queued": False, "operation": serialize_operation(existing)}
    with audit_operation(operation=REFRESH, principal=principal):
        resource = _source(principal)
        operation, created = enqueue(
            resource,
            models.OperationRequest.Action.SCAN,
            idempotency_key=key,
            requested_actor=principal.actor,
            requested_interface=principal.interface,
            requested_device=principal.device,
            requested_person_key=principal.person_key,
            reason=command.reason,
            input={"generation": resource.generation},
        )
    if created:
        # The worker pulls the scan over the bridge; the ring only says one
        # waits, and only once it is stored for the claim to find.
        transaction.on_commit(ring_automation_doorbell)
    return {"ok": True, "queued": created, "operation": serialize_operation(operation)}


# ----------------------------------------------------------- how it stands


@dataclass(frozen=True, slots=True)
class Reviewed:
    """One proposal as a person decides on it: what adopting it changes,
    each way that lets a run do more, and every reason HQ will not adopt it."""

    proposal: SourceProposal
    # None when it is the commit adopted now.
    preview: ChangePreview | None
    widens: tuple[str, ...] = ()
    # What no playbook's row of ``preview`` carries, said above it: the ways
    # it widens and the commit's own reasons HQ won't adopt it.
    unmarked: tuple[str, ...] = ()
    unplaced: tuple[str, ...] = ()
    # Every reason HQ will not adopt it, each playbook's by its sidecar.
    refused: tuple[str, ...] = ()
    # Whether it is the catalog adopted now, so adopting it changes nothing.
    adopted: bool = False

    @property
    def revision(self) -> str:
        return self.proposal.revision[:SHORT_REVISION]

    @property
    def pending(self) -> bool:
        """Whether it waits for a person: neither adopted nor rejected."""

        return not self.adopted and self.proposal.rejected_at is None


@dataclass(frozen=True, slots=True)
class SourceStanding:
    """Where the automation source stands: its newest scan and how it went,
    the newest proposal reviewed, and the catalog adopted now."""

    scan: OperationRequest | None
    scanned: Standing
    # The newest scan that ended, and how it went: whether HQ's worker
    # reached the repository.
    reached: OperationRequest | None
    reach: Standing
    proposal: Reviewed | None
    catalog: AdoptedCatalog | None
    procedures: tuple[Any, ...]

    @property
    def revision(self) -> str:
        return self.catalog.revision[:SHORT_REVISION] if self.catalog else ""

    @property
    def pending(self) -> Reviewed | None:
        """The newest proposal, while it waits for a person to adopt or reject it."""

        return self.proposal if self.proposal is not None and self.proposal.pending else None


def _scans():
    """The source's scans, each with the state Jobs shows it in."""

    return models.OperationRequest.objects.filter(
        resource__key=SOURCE_KEY, action=models.OperationRequest.Action.SCAN
    ).annotate(seen=run_seen_state(timezone.now()))


def scan_standing(operation_id: str) -> Standing | None:
    """How one scan stands, or None when the source has no scan by that id."""

    found = _scans().filter(pk=operation_id).first()
    return _stands(found) if found is not None else None


def _stands(scan: OperationRequest) -> Standing:
    """How a scan stands, a queued one saying what it waits on: the
    automation worker, and once it has not come, when that last took any work
    and what to do. A worker busy with other work is coming."""

    standing = asks.operation_standing(scan)
    if standing.state != asks.QUEUED:
        return standing
    worked = models.OperationRequest.objects.filter(
        action__in=(models.OperationRequest.Action.SCAN, models.OperationRequest.Action.RUN)
    )
    now = timezone.now()
    if worked.filter(state=models.OperationRequest.State.CLAIMED, lease_expires_at__gt=now).exists():
        return replace(standing, note=WORKER_DUE)
    last = worked.exclude(claimed_at=None).order_by("-claimed_at").values_list("claimed_at", flat=True).first()
    seen = WORKER_SEEN.format(ago=ago(last)) if last else WORKER_NEVER
    late = now - scan.created_at > CLAIM_EXPECTED
    return replace(standing, note=WORKER_ABSENT.format(seen=seen) if late else WORKER_DUE)


def reviewed(proposal: SourceProposal) -> Reviewed:
    """``proposal`` as a person decides on it, compared with the catalog adopted now."""

    planned = automation_adoption.judged(proposal.pk)
    if planned.digest == planned.current and not planned.refused:
        return Reviewed(proposal, None, adopted=True)
    compared = planned.compared
    # A playbook HQ can't adopt is marked as one that widens is, its row
    # saying why.
    unadoptable = tuple(f"{named} can't be adopted." for named in planned.unadoptable)
    shown, unmarked = marked(
        grouped(compared["before"], compared["after"], label="Changes"), (*planned.widens, *unadoptable)
    )
    return Reviewed(
        proposal,
        shown,
        widens=planned.widens,
        unmarked=tuple(each for each in unmarked if each not in unadoptable),
        unplaced=planned.unplaced,
        refused=planned.refused,
    )


def standing(proposal: int | None = None) -> SourceStanding:
    """Where the automation source stands, with the proposal ``proposal``
    reviewed, or the newest one when none is named; RecordNotFound for a
    proposal it no longer holds."""

    proposals = models.SourceProposal.objects.order_by("-pk")
    found = proposals.filter(pk=proposal).first() if proposal is not None else proposals.first()
    if proposal is not None and found is None:
        raise RecordNotFound("No scan result has that ID. Refresh to scan again.")
    scans = _scans().order_by("-created_at")
    scan = scans.first()
    scanned = _stands(scan) if scan is not None else asks.Standing()
    # While a newer scan is under way, what the page says of the repository's
    # reach is still the last one that ended.
    under_way = (models.OperationRequest.State.QUEUED, models.OperationRequest.State.CLAIMED)
    reached = scans.exclude(state__in=under_way).first() if scanned.live else scan
    catalog = automation_adoption.adopted()
    return SourceStanding(
        scan=scan,
        scanned=scanned,
        reached=reached,
        reach=asks.operation_standing(reached) if reached is not None else asks.Standing(),
        proposal=reviewed(found) if found is not None else None,
        catalog=catalog,
        procedures=automation_adoption.procedures(catalog.digest) if catalog else (),
    )


def decisions(pending: Reviewed | None, principal: Principal, *, diff: bool) -> tuple[PageAction, ...]:
    """What a person may do about the commit waiting to be adopted, in the
    order the Playbooks page and its scan's page draw them: View diff (on
    the Playbooks page, ``diff``), Adopt all when HQ can adopt every
    playbook, and Reject all. Nothing when no commit waits."""

    if pending is None:
        return ()
    proposal = pending.proposal
    shown: list[PageAction] = []
    if diff:
        shown.append(PageAction("View diff", run_url(str(proposal.operation_id))))
    if not pending.refused and capability_action_link(automation_adoption.ADOPT, principal=principal):
        adopt = reverse("control_plane:playbooks_adopt", kwargs={"pk": proposal.pk})
        shown.append(PageAction("Adopt all", adopt, method="post"))
    if capability_action_link(automation_adoption.REJECT, principal=principal):
        reject = reverse("control_plane:playbooks_reject", kwargs={"pk": proposal.pk})
        shown.append(PageAction("Reject all", reject, method="post"))
    return tuple(shown)


def capability_specs() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            REFRESH,
            "Scan your repository for playbooks and keep the scan result. Nothing in it runs until a person adopts it.",
            "remote_write",
            Capability.MANAGE_INFRASTRUCTURE,
            RefreshCommand,
            refresh,
            execution_notes=(
                "Queue one scan, or answer the one already waiting.",
                "HQ reads the newest commit on the default branch and runs none of it.",
                "Keep the scan result, with each way a playbook falls short.",
            ),
            label="Refresh playbooks",
            verb=models.OperationRequest.Action.SCAN.label,
            wants="scan your repository for playbooks",
            home=PLAYBOOKS,
        ),
    )
