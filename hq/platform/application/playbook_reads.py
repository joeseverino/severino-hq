"""The adopted playbooks and the scans of their repository as readable
resources, for an agent to find what to run, where, and what waits for a person to adopt.

Each playbook is read from the catalog its commands are built from
(``automation_runs.capability_specs``), with the facts its Run page states
(``automation_runs.about``), and each scan result as the Playbooks page
reviews it (``automation_sources.standing``), so a read says what the pages
say. What a page does not show is the machines a playbook can run on now:
the ones the worker's newest scan says it has a login to as the playbook's
profile, which an ask is held to (``automation_runs.reached``).
"""

from typing import Annotated, Any, TypedDict, get_args

lazy from hq.domains.control_plane.models import SourceProposal
lazy from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND

from . import automation_adoption, automation_runs
from .automation_manifest import OperationManifest
from .automation_sources import PLAYBOOKS as PAGE, Reviewed, standing
from .describe import Describe, listed
from .integration_specs import CommandFact, ResourceSpec
from .projection import complete, iso
from .record_errors import RecordNotFound
from .resources import EmptyQuery
from .security import Capability, Principal
from .ui import ended

PLAYBOOKS = "playbooks"
SCANS = "playbook_scans"

_EFFECTS = get_args(OperationManifest.model_fields["effect"].annotation)


class PlaybookRow(TypedDict):
    playbook: Annotated[str, Describe("The playbook's id, which a read of one and the `runs` filter `playbook` take.")]
    title: Annotated[str, Describe("Its name in HQ, as its Run page is headed.")]
    capability: Annotated[
        str, Describe("The command that runs it, by name. Its schema says what it takes, and its notes what it does.")
    ]
    effect: Annotated[
        str,
        Describe(
            ended(f"What it does to a machine: {listed(_EFFECTS)}")
            + " Anything but a read waits for an operator's approval when an agent asks."
        ),
    ]
    reachable: Annotated[
        list[str],
        Describe(
            "The machines a run of it may name now, each by its name in HQ. HQ's runner has a login to each "
            "as it needs. A Refresh reads them anew."
        ),
    ]


class PlaybookFact(TypedDict):
    label: Annotated[str, Describe("What the fact is about, as its Run page labels it.")]
    value: Annotated[str, Describe("The fact in the words its Run page says it in.")]


class PlaybookRecord(PlaybookRow):
    """One playbook whole: its row, and what its Run page states of it."""

    facts: Annotated[
        list[PlaybookFact],
        Describe("What its Run page states of it, each fact by its label, such as where it runs and its timeout."),
    ]


class ScanResult(TypedDict):
    id: Annotated[int, Describe("The scan result's id.")]
    revision: Annotated[str, Describe("The commit it read, by its full id.")]
    branch: Annotated[str, Describe("The branch that commit is on.")]
    committer: Annotated[str, Describe("Who committed it, as GitHub names them; empty when it said no one.")]
    read_at: Annotated[str | None, Describe("When HQ read it, an ISO 8601 date-time.")]
    adopted: Annotated[
        bool, Describe("Whether it is the commit HQ runs playbooks from now, so adopting it does nothing.")
    ]
    refused: Annotated[list[str], Describe("Each reason HQ will not adopt it; empty when it can.")]
    widens: Annotated[list[str], Describe("Each way adopting it lets a run do more than it may now.")]
    pending: Annotated[
        bool, Describe("Whether it waits for a person to adopt or reject it in HQ. Only a person decides it.")
    ]


def _manifests() -> tuple[OperationManifest, ...]:
    return tuple(automation_runs.operation_registry().values())


def _reachable(manifest: OperationManifest, reaches: frozenset[tuple[str, str]]) -> list[str]:
    """The machines ``manifest`` is written for that the worker reaches as its profile."""

    profile = automation_runs.login_profile(manifest)
    return sorted(host for host, reached in reaches if reached == profile and manifest.admits(MACHINE_KIND, host))


def _row(manifest: OperationManifest, titles: dict[str, str], reaches: frozenset[tuple[str, str]]) -> dict[str, Any]:
    return {
        "playbook": manifest.id,
        "title": automation_runs.run_title(manifest.id, titles),
        "capability": automation_runs.capability_name(manifest.id),
        "effect": manifest.effect,
        "reachable": _reachable(manifest, reaches),
    }


def _fact(fact: CommandFact) -> dict[str, str]:
    # A fact that names machines draws each as its link, so its words are their names.
    return {"label": fact.label, "value": fact.value or ", ".join(link.label for link in fact.links)}


def list_playbooks(**_caller: Principal) -> dict[str, Any]:
    """Every adopted playbook, in the catalog's order."""

    titles, reaches = automation_adoption.playbook_titles(), automation_runs.runner_reaches()
    return complete([_row(manifest, titles, reaches) for manifest in _manifests()])


def get_playbook(playbook_id: str, *, principal: Principal) -> dict[str, Any]:
    """One adopted playbook by its id, with its Run page's facts as ``principal`` reads them."""

    try:
        manifest = automation_runs.operation_registry()[playbook_id]
    except KeyError:
        raise RecordNotFound("No adopted playbook has that id.") from None
    row = _row(manifest, automation_adoption.playbook_titles(), automation_runs.runner_reaches())
    return {**row, "facts": [_fact(fact) for fact in automation_runs.about(manifest, principal).facts]}


def _scan(reviewed: Reviewed, newest: bool) -> dict[str, Any]:
    proposal = reviewed.proposal
    return {
        "id": proposal.pk,
        "revision": proposal.revision,
        "branch": proposal.branch,
        "committer": proposal.committer,
        "read_at": iso(proposal.created_at),
        "adopted": reviewed.adopted,
        "refused": list(reviewed.refused),
        "widens": list(reviewed.widens),
        "pending": newest and reviewed.pending,
    }


def _scanned(proposal: int | None) -> dict[str, Any] | None:
    sources = standing(proposal)
    if sources.proposal is None:
        return None
    newest = proposal is None or not SourceProposal.objects.filter(pk__gt=proposal).exists()
    return _scan(sources.proposal, newest)


def list_scans() -> dict[str, Any]:
    """The newest scan result, the one there is to decide on; none before the first Refresh."""

    newest = _scanned(None)
    return complete([newest] if newest is not None else [])


def get_scan(scan_id: int) -> dict[str, Any]:
    """One scan result by its id."""

    found = _scanned(scan_id)
    if found is None:
        raise RecordNotFound("No scan result has that ID. Refresh to scan again.")
    return found


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            PLAYBOOKS,
            "Playbooks",
            "Every adopted playbook: the command that runs it, what it does, and the machines it can run on now.",
            Capability.READ,
            list_playbooks,
            EmptyQuery,
            get_playbook,
            "playbook",
            web_route=PAGE,
            pass_principal=True,
            row_type=PlaybookRow,
            record_type=PlaybookRecord,
        ),
        ResourceSpec(
            SCANS,
            "Playbook scans",
            "The newest scan of your playbooks' repository, or one by its id. It says what adopting it widens; "
            "a person adopts or rejects it in HQ.",
            Capability.READ,
            list_scans,
            EmptyQuery,
            get_scan,
            "id",
            identifier_type=int,
            web_route=PAGE,
            row_type=ScanResult,
            record_type=ScanResult,
        ),
    )
