"""Asking HQ's runner which playbooks its repository declares whenever that
repository's default branch moves, and storing what it found as a scan result
HQ judged.

Nothing is fetched here: the worker reads the repository with the token only it
holds and reports over the bridge. A proposal is never adopted and never
runnable. How the scan reads a repository: docs/Architecture.md, "Scanning
the playbooks".
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Any

from django.utils import timezone
from django.utils.text import Truncator
from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

lazy from hq.domains.control_plane import models
lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND
lazy from hq.platform.core.audit import audited_labels
lazy from hq.platform.core.models import AuditLog
lazy from hq.platform.core.templatetags.change_tags import change_mark

from .automation_manifest import DottedName, ProcedureTerms
from .command_fields import Checked, Reason, RequiredText
from .contracts import Closed
from .security import Capability, Principal
from .titled import Titled
from .ui import ended
lazy from . import asks, automation_adoption, pages
lazy from .action_links import capability_action_link, command_url
lazy from .approval_review import grouped, marked
lazy from .automation_runs import READ_PROFILE, access_words, capability_name
lazy from .cadence import ask, automation_standing
lazy from .entity_links import entity_link
lazy from .findings import rule_for
lazy from .github_estate import repository_for
lazy from .infrastructure import ManagedResourceCommand, save_sourced
lazy from .playbook_reach import reachable
lazy from .record_errors import RecordNotFound
lazy from .resource_operations import Prepared, asked_fields, enqueue
lazy from .routes import reverse
lazy from .tables import Cell, DataTable, Row, TableColumn
lazy from .work_runs import UNENDED, run_rows, run_url, seen_as_runs, standing_of

if TYPE_CHECKING:
    from hq.domains.control_plane.models import (
        AdoptedCatalog,
        ManagedResource,
        OperationRequest,
        SourceProposal,
    )

    from .approval_review import ChangePreview
    from .asks import Standing
    from .automation_adoption import PlaybookChange
    from .pages import PageAction

# HQ's own act of asking for a scan, as its audit rows name it.
SCAN = Titled("automation.source.scan", "Asked for a scan of the playbooks")
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


def _pattern(value: str) -> str:
    try:
        re.compile(value)
    except re.error:
        raise ValueError from None
    return value


class InputBound(Closed):
    """What Ansible's argument spec cannot say of a text input: the shape it
    must have and how long it may be."""

    model_config = ConfigDict(frozen=True, strict=True)

    pattern: Annotated[
        RequiredText,
        StringConstraints(max_length=500),
        Checked(_pattern, Reason("input_pattern", "must be a regular expression")),
    ]
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


def _declaration_problems(declaration: Any) -> list[str]:
    if declaration is None:
        return []
    try:
        # As JSON, which the sidecar was read into: a list is a strict tuple there.
        ProcedureDeclaration.model_validate_json(json.dumps(declaration))
    except ValidationError as exc:
        return [
            Truncator(
                ended(f"Its sidecar's {'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}")
            ).chars(PROBLEM_LIMIT)
            for error in exc.errors(include_url=False)
        ]
    return []


def _unknown_rules(declaration: Any) -> list[str]:
    """Each finding rule a sidecar names that HQ raises none of."""

    if not isinstance(declaration, dict):
        return []
    return [
        f"Its sidecar's {field} names {rule}, which is no finding HQ raises."
        for field in ("fixes", "investigates")
        if isinstance(rules := declaration.get(field), list)
        for rule in rules
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
        problems += _unknown_rules(declaration)
        found.append({**procedure, "problems": problems[:MOST_PROBLEMS]})
    return found


def proposal(catalog: dict[str, Any]) -> SourceProposal:
    """What one scan found, held to the bridge contract at its door, as HQ
    judges it, unsaved."""

    verification = catalog["verification"]
    return models.SourceProposal(
        repository=catalog["repository"],
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


def propose(operation: OperationRequest, catalog: dict[str, Any]) -> SourceProposal:
    """Store what one scan found."""

    found = proposal(catalog)
    found.operation = operation
    found.save(force_insert=True)
    return found


def _source(principal: Principal) -> ManagedResource:
    if not models.ManagedResource.objects.filter(key=SOURCE_KEY).exists():
        save_sourced(
            ManagedResourceCommand(key=SOURCE_KEY, kind=SOURCE_KIND, spec={"named_by": "host"}), principal=principal
        )
    return models.ManagedResource.objects.select_for_update().get(key=SOURCE_KEY)


def _moved_past(newest: SourceProposal | None) -> str | None:
    """The head of the source's default branch when the newest scan result
    is not of it; "" while no scan result names its repository, so one is
    due; None when it is of the head, or the head was not read."""

    if newest is None or not newest.repository:
        return ""
    found = repository_for(newest.repository)
    head = str(((found.head if found else None) or {}).get("sha", ""))
    return head if head and head != newest.revision else None


def scan_when_due(*, principal: Principal) -> OperationRequest | None:
    """``queue_scan`` as HQ, ``principal``, when the source's default branch
    has moved past the newest scan result's commit, or no scan result names
    its repository yet; once a day for each head, so a scan that failed is
    tried again the next day.

    Asked once a reading of the repositories is stored, which a push to the
    repository makes due, so a push is scanned with nobody asking.
    """

    head = _moved_past(automation_adoption.scan_result())
    if head is None:
        return None
    return queue_scan(principal=principal, once=f"scan:{head or 'unnamed'}:{timezone.now().date().isoformat()}")


def queue_scan(*, principal: Principal, once: str) -> OperationRequest | None:
    """Queue one scan of the automation source asked by ``principal``, once
    for ``once``, and ring for the worker; the scan queued, else None: one
    already waits, or no worker would take it. Its door binds it as ``SCAN``."""

    # A scan no worker will take would wait for nothing: the finding that
    # says so (``automation_findings``) carries how to set one up.
    if automation_standing(READ_PROFILE).cannot("scan", now=timezone.now()):
        return None

    def prepare() -> Prepared:
        resource = _source(principal)
        return resource, asked_fields(principal, reason="", generation=resource.generation)

    operation, created = enqueue(models.OperationRequest.Action.SCAN, prepare, once=once)
    if not created:
        return None
    # The worker pulls the scan over the bridge; the ring only says one
    # waits, and only once it is stored for the claim to find.
    ask(doorbell="automation")
    return operation


# ----------------------------------------------------------- how it stands


@dataclass(frozen=True, slots=True)
class Reviewed:
    """One proposal as a person decides on it: each playbook it changes,
    what adopting them changes, each way that lets a run do more, and every
    reason HQ will not adopt it."""

    proposal: SourceProposal
    # None when nothing of it waits.
    preview: ChangePreview | None
    # Whether no scan result is newer, so it is the one there is to decide on.
    newest: bool
    changes: tuple[PlaybookChange, ...] = ()
    widens: tuple[str, ...] = ()
    # What no playbook's row of ``preview`` carries, said above it: the ways
    # it widens and the commit's own reasons HQ won't adopt it.
    unmarked: tuple[str, ...] = ()
    unplaced: tuple[str, ...] = ()
    # Every reason HQ will not adopt it, each playbook's by its sidecar.
    refused: tuple[str, ...] = ()

    @property
    def revision(self) -> str:
        return self.proposal.revision[:SHORT_REVISION]

    @property
    def adopted(self) -> bool:
        """Whether every playbook of it is adopted as it reads, or rejected, so nothing of it waits."""

        return not self.changes

    @property
    def pending(self) -> bool:
        """Whether it waits for a person: the newest scan result, with a
        change neither adopted nor rejected."""

        return self.newest and not self.adopted and self.proposal.rejected_at is None


@dataclass(frozen=True, slots=True)
class SourceStanding:
    """Where the automation source stands: its newest scan and how it went,
    the newest proposal reviewed, and the catalog adopted now."""

    scan: OperationRequest | None
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

    return seen_as_runs(
        models.OperationRequest.objects.filter(
            resource__key=SOURCE_KEY, action=models.OperationRequest.Action.SCAN
        ).select_related("resource")
    )


def reviewed(proposal: SourceProposal) -> Reviewed:
    """``proposal`` as a person decides on it, compared with the catalog adopted now."""

    planned = automation_adoption.judged(proposal.pk)
    newest = automation_adoption.scan_result() == proposal
    if not planned.changes:
        return Reviewed(proposal, None, newest)
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
        newest,
        changes=planned.changes,
        widens=planned.widens,
        unmarked=tuple(each for each in unmarked if each not in unadoptable),
        unplaced=planned.unplaced,
        refused=planned.refused,
    )


def standing(proposal: int | None = None) -> SourceStanding:
    """Where the automation source stands, with the proposal ``proposal``
    reviewed, or the newest one when none is named; RecordNotFound for a
    proposal it no longer holds."""

    found = automation_adoption.scan_result(proposal)
    if proposal is not None and found is None:
        raise RecordNotFound("No scan result has that ID.")
    scans = _scans().order_by("-created_at")
    scan = scans.first()
    # While a newer scan has not ended, started or not, what the page says of
    # the repository's reach is still the last one that ended.
    reached = scans.exclude(state__in=UNENDED).first() if scan is not None and scan.state in UNENDED else scan
    catalog = automation_adoption.adopted()
    return SourceStanding(
        scan=scan,
        reached=reached,
        reach=standing_of(run_rows((reached,))[0]) if reached is not None else asks.Standing(),
        proposal=reviewed(found) if found is not None else None,
        catalog=catalog,
        procedures=automation_adoption.procedures(catalog.digest) if catalog else (),
    )


def adopted_revision() -> str:
    """The commit of the newest adoption, by the characters a page names it by; empty before any."""

    catalog = automation_adoption.adopted()
    return catalog.revision[:SHORT_REVISION] if catalog else ""


def entry_url(proposal: SourceProposal) -> str:
    """The audit entry that records what a scan read, which says what it
    changes and offers a person's decision on it; the scan's run page for
    one read before its rows were kept."""

    entry = (
        AuditLog.objects.filter(
            object_type=audited_labels()[models.SourceProposal],
            object_id=str(proposal.pk),
            action=AuditLog.Action.CREATED,
        )
        .values_list("pk", flat=True)
        .first()
    )
    return reverse("core:audit_detail", args=[entry]) if entry else run_url(str(proposal.operation_id))


def of_audit_entry(event: AuditLog) -> Reviewed | None:
    """The scan result an audit row records being read, reviewed; None for any other row."""

    if event.action != AuditLog.Action.CREATED or event.object_type != audited_labels()[models.SourceProposal]:
        return None
    found = models.SourceProposal.objects.filter(pk=int(event.object_id)).first() if event.object_id.isdigit() else None
    return reviewed(found) if found is not None else None


def _deciding(principal: Principal) -> tuple[bool, bool]:
    """Whether ``principal`` may adopt, and whether they may reject."""

    return (
        capability_action_link(automation_adoption.ADOPT, principal=principal) is not None,
        capability_action_link(automation_adoption.REJECT, principal=principal) is not None,
    )


def decisions(pending: Reviewed | None, principal: Principal, *, diff: bool) -> tuple[PageAction, ...]:
    """What a person may do about the commit waiting to be adopted, in the
    order the Playbooks page and its scan's audit entry draw them: View diff
    (on the Playbooks page, ``diff``, for a reader of the audit log), Adopt
    all, or Adopt all HQ can while it can't adopt some, and Reject all.
    Nothing when no commit waits."""

    if pending is None or not pending.pending:
        return ()
    proposal = pending.proposal
    adopts, rejects = _deciding(principal)
    shown: list[PageAction | None] = []
    if diff and principal.permits(Capability.READ_AUDIT_LOG):
        shown.append(pages.PageAction("View diff", entry_url(proposal)))
    adoptable = not pending.unplaced and any(not change.problems for change in pending.changes)
    if adoptable and adopts:
        variant = automation_adoption.ADOPTABLE if pending.refused else "all"
        shown.append(pages.PageAction.command(automation_adoption.ADOPT, proposal.pk, variant=variant))
    if rejects:
        shown.append(pages.PageAction.command(automation_adoption.REJECT, proposal.pk, variant="all"))
    return tuple(action for action in shown if action is not None)


def playbook_table(
    adopted: tuple[Any, ...], review: Reviewed | None, principal: Principal, *, every: bool = True
) -> DataTable:
    """The table of each playbook adopted (``every``) or only each one
    changed, and each one a waiting scan (``review``) adds, in the order of
    their titles: as adopted, or as the scan would bring it, and while the
    scan waits, how it changes each, with what ``principal`` may do about it."""

    changes = {change.key: change for change in review.changes} if review is not None and review.pending else {}
    adopts, rejects = _deciding(principal)
    rows = [
        _row(procedure, changes.get(procedure.id), review, adopts=adopts, rejects=rejects, adopted=True)
        for procedure in adopted
        if every or procedure.id in changes
    ]
    held = {procedure.id for procedure in adopted}
    rows.extend(
        _row(change.procedure, change, review, adopts=adopts, rejects=rejects, title=change.title)
        for key, change in changes.items()
        if key not in held
    )
    deciding = bool(changes)
    columns = (
        TableColumn("Playbook"),
        TableColumn("Effect", drop=3),
        TableColumn("Runs on", kind="identifier", drop=1),
        TableColumn("Access", drop=2),
        *((TableColumn("Status", kind="status"),) if deciding else ()),
    )
    shown = sorted(rows, key=lambda row: row[0])
    return DataTable(
        "Playbooks",
        columns,
        tuple(replace(row, cells=row.cells[: len(columns)]) for _title, row in shown),
        region={"empty": "No playbooks are adopted yet."},
    )


def _row(
    procedure: Any,
    change: PlaybookChange | None,
    review: Reviewed | None,
    *,
    adopts: bool,
    rejects: bool,
    adopted: bool = False,
    title: str = "",
) -> tuple[str, Row]:
    """One playbook's row by its title: ``procedure`` as adopted
    (``adopted``) or as the scan brings it, None for one HQ can't adopt,
    which is its ``title`` alone. Its adopted title links its command's
    page; one the scan brings has no page yet. A change HQ can't adopt says
    why beneath its title."""

    actions: list[PageAction | None] = []
    if change is not None and review is not None:
        sent = {"playbook": change.key}
        if adopts and not change.problems and not review.unplaced:
            actions.append(pages.PageAction.post(automation_adoption.ADOPT, review.proposal.pk, sends=sent))
        if rejects:
            actions.append(pages.PageAction.post(automation_adoption.REJECT, review.proposal.pk, sends=sent))
    kind = change.change if change is not None else ""
    state = Cell(status=change_mark(kind)) if change_mark(kind) else Cell()
    shown = tuple(filter(None, actions))
    data = {"change": kind} if kind else {}
    # Each reason HQ can't adopt it, under its name, where every width shows them.
    refused = tuple(Cell(problem) for problem in change.problems) if change is not None else ()
    if procedure is None:
        # HQ kept nothing of it but its title and its problems, so they stand in for its facts.
        empty = Cell(blank=True)
        return title, Row((Cell(title, lines=refused), empty, empty, empty, state), actions=shown, data=data)
    url = command_url(capability_name(procedure.id)) if adopted else ""
    hosts = tuple(Cell.of(entity_link("machine", host)) for host in reachable(procedure))
    return procedure.title, Row(
        (
            Cell(procedure.title, url, lines=refused),
            Cell(procedure.effect.label),
            Cell(lines=hosts),
            Cell(access_words(procedure.privilege)),
            state,
        ),
        actions=shown,
        data=data,
    )
