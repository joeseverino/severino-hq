"""Adopting a proposal: one scanned commit of the automation source becomes the
catalog HQ runs procedures from, once an operator agrees.

How a commit is judged, what is refused and how an adoption reaches every
process: docs/Architecture.md, "Adopting the playbooks". Adoption is a person's
act: ``adopt_playbooks`` is held only at the operator's own interfaces
(``security.HELD_ONLY_AT``), so an agent's adoption is refused, never held.
"""

import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypedDict

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.text import capfirst
from pydantic import (
    Field,
    Strict,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from hq.domains.control_plane.models import AdoptedCatalog, SourceProposal
from hq.platform.core.digests import digest

from . import automation_sources
from .answer_fields import CallMessage
from .automation_inputs import INPUT_NAME, AdoptedInput, input_type, procedure_inputs
from .automation_manifest import AUTHORED, PLAYBOOK, REVISION, OperationManifest, ProcedureTerms, Statement
from .automation_sources import ProcedureDeclaration
from .command_fields import Describe, RequiredText
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .projection import read_once
from .quoted import Quoted
from .record_errors import PolicyError, RecordNotFound
from .record_fields import RecordId
from .security import Capability, Principal
lazy from . import agent_registry
lazy from .approval_review import SUMMARY
lazy from .automation_manifest import REVISION_INPUT
lazy from .automation_runs import RETRY_WORDS, access_words, anywhere, reading_words, timeout_words
lazy from .findings import rule_for
lazy from .labels import dotted, lower_first
lazy from .payload_words import field_words
lazy from .ui import ended
lazy from .work_runs import wording

ADOPT = "automation.source.adopt"
# Adopt's words for every change that waits while some of them HQ can't adopt.
ADOPTABLE = "adoptable"
REJECT = "automation.source.reject"
# What a playbook HQ can't adopt is, at a glance, on its row of a comparison.
CANT_ADOPT = "HQ can't adopt it"
# The kinds of a path a proposal may carry and still be adopted. A
# configuration or a plugin directory is code no procedure's review reads.
REFUSED_KINDS = {
    "config": "an ansible.cfg, which changes how every playbook runs",
    "plugins": "a plugins directory, whose code runs with every playbook",
}
# Where a role's own files sit, beside a playbook or at the project's root.
ROLES = "roles"

# The most characters of an object id a comparison shows: enough to tell two apart.
SHOWN_OBJECT = 12


class AdoptedProcedure(ProcedureTerms):
    """One procedure of an adopted catalog: what its sidecar declares, what
    Ansible says of it, the commit it runs at, and the files it runs and the
    ones of that commit no playbook runs (``shared``), each by its object id."""

    playbook: Annotated[str, StringConstraints(pattern=PLAYBOOK)]
    title: Annotated[str, StringConstraints(max_length=200), AUTHORED]
    preconditions: tuple[Statement, ...]
    verification: tuple[Statement, ...]
    inputs: dict[Annotated[str, StringConstraints(pattern=INPUT_NAME.pattern)], AdoptedInput]
    revision: Annotated[str, StringConstraints(pattern=REVISION)]
    files: dict[str, str]
    shared: dict[str, str]

    def same(self, other: AdoptedProcedure) -> bool:
        """Whether ``other`` runs what this does: the commit apart, every
        term and every file the same."""

        return self.model_dump(exclude={"revision"}) == other.model_dump(exclude={"revision"})


_ADOPTED = TypeAdapter(tuple[AdoptedProcedure, ...])

# ---------------------------------------------------------------- the catalog


@dataclass(frozen=True)
class _Known:
    digest: str
    procedures: tuple[AdoptedProcedure, ...]
    manifests: tuple[OperationManifest, ...]


class _ThisProcess:
    """The catalog this process builds its commands from: None until it first
    reads one, then what its newest read found."""

    known: _Known | None = None


_THIS_PROCESS = _ThisProcess()


def generation() -> str:
    """The digest of the catalog this process builds its commands from, or ""
    before any adoption: what it last read, read now the first time.

    A request served (``served``) catches up on its first read, so a process
    builds its commands from an adoption another process made at its next
    request that needs them, and one request is served from one catalog.
    """

    pending = _UNCHECKED.get()
    if pending is not None and not pending[0]:
        pending[0] = True
        return catch_up()
    known = _THIS_PROCESS.known
    return known.digest if known is not None else catch_up()


def held() -> str:
    """The digest of the catalog this process last read, "" before its first
    read, asking the database nothing: for what is the same in every
    generation (``integrations.held_graph``)."""

    known = _THIS_PROCESS.known
    return known.digest if known is not None else ""


# Set by a request's door: whether this request has read the newest catalog yet.
_UNCHECKED: ContextVar[list[bool] | None] = ContextVar("hq_adopted_catalog_checked", default=None)


@contextmanager
def served() -> Iterator[None]:
    """Serve one web request (a page or an API call) from the catalog
    adopted now: its first read of the generation reads the newest, and a
    request that builds no command reads nothing."""

    with _UNCHECKED.set([False]):
        yield


def catch_up() -> str:
    """Read the newest catalog now, and build this process's commands from it
    once it is another: in a web request served, at each MCP tool call, once
    an adoption commits, and for a seed, which adopts inside a transaction
    that has not committed."""

    digest = _newest_digest()
    known = _THIS_PROCESS.known
    if known is None or known.digest != digest:
        _THIS_PROCESS.known = _load(digest)
    return digest


def forget() -> None:
    """Start this process again from no catalog, as a test does, without a
    read: the next request's door reads the database."""

    _THIS_PROCESS.known = _Known("", (), ())


def _newest_digest() -> str:
    try:
        return AdoptedCatalog.objects.values_list("digest", flat=True).first() or ""
    except DatabaseError:
        # Composed before the database is migrated (a system check, a
        # migration's own run), there is no catalog yet.
        return ""


def adopted() -> AdoptedCatalog | None:
    """The catalog HQ runs from now, or None before any adoption: read once
    for every part of a page that names it."""

    return read_once("automation.adopted", AdoptedCatalog.objects.first)


def manifests(digest: str) -> tuple[OperationManifest, ...]:
    """Each procedure of the catalog adopted with ``digest``, as HQ runs it."""

    return _catalog(digest).manifests


def procedures(digest: str | None = None) -> tuple[AdoptedProcedure, ...]:
    """Each procedure of the catalog adopted with ``digest``, as adopted: the
    terms its manifest leaves out, such as its inputs' argument spec. Read at
    this process's generation when not given."""

    return _catalog(generation() if digest is None else digest).procedures


def _catalog(digest: str) -> _Known:
    known = _THIS_PROCESS.known
    if known is not None and known.digest == digest:
        return known
    return _load(digest)


def _load(digest: str) -> _Known:
    catalog = AdoptedCatalog.objects.filter(digest=digest).first() if digest else None
    if catalog is None:
        return _Known(digest, (), ())
    rows = adopted_procedures(catalog)
    return _Known(digest, rows, tuple(manifest(row) for row in rows))


def adopted_procedures(catalog: AdoptedCatalog) -> tuple[AdoptedProcedure, ...]:
    """``catalog``'s procedures, read as JSON, which the row is stored as: a
    list is a strict tuple there."""

    return _ADOPTED.validate_json(json.dumps(catalog.procedures))


def manifest(procedure: AdoptedProcedure) -> OperationManifest:
    """The manifest HQ runs ``procedure`` from, at the commit it was adopted from."""

    return OperationManifest(
        **procedure.model_dump(include=set(ProcedureTerms.model_fields)),
        revision=procedure.revision,
        playbook=procedure.playbook,
        input_type=input_type(procedure.id, procedure.inputs),
        preconditions=procedure.preconditions,
        verification=procedure.verification,
    )


# ------------------------------------------------------------ judging a commit


def _role_files(playbook: str, roles: Iterable[str], paths: Iterable[str]) -> set[str]:
    """The files of the roles a playbook lists, wherever Ansible finds them."""

    places = [f"{ROLES}/{role}/" for role in roles]
    beside = PurePosixPath(playbook).parent
    if str(beside) != ".":
        places += [f"{beside}/{ROLES}/{role}/" for role in roles]
    return {path for path in paths if any(path.startswith(place) for place in places)}


@dataclass(frozen=True, slots=True)
class _Unadoptable:
    """A playbook of a scan HQ can't adopt: the key and title its row is
    compared by, every problem its sidecar has, and the files it would run,
    which are its own rather than every playbook's."""

    key: str
    title: str
    problems: tuple[str, ...]
    files: frozenset[str]


def _procedures(proposal: SourceProposal) -> tuple[dict[str, AdoptedProcedure], dict[str, _Unadoptable]]:
    """Each playbook of the scan HQ could adopt, by its id, and each one it
    can't, by its sidecar."""

    objects = {entry["path"]: entry["object"] for entry in proposal.files}
    used = {scanned["sidecar"]: _used(scanned, objects) for scanned in proposal.procedures}
    # The files no playbook of the commit runs, which any of them may read.
    every = {path for paths in used.values() for path in paths}
    shared = {path: name for path, name in sorted(objects.items()) if path not in every}
    found: dict[str, AdoptedProcedure] = {}
    unadoptable: dict[str, _Unadoptable] = {}
    sidecars: dict[str, str] = {}
    # What each playbook's play asserts after its work, by its id: also the
    # verification of a change it settles (``settled_by``), whose run HQ
    # holds until this read reports.
    asserted = {
        scanned["declaration"]["id"]: tuple(scanned["verification"])
        for scanned in proposal.procedures
        if isinstance(scanned["declaration"], dict) and isinstance(scanned["declaration"].get("id"), str)
    }
    for scanned in proposal.procedures:
        name = scanned["sidecar"]
        declared = scanned["declaration"].get("id") if isinstance(scanned["declaration"], dict) else None
        problems = list(scanned["problems"])
        procedure = None if problems else _procedure(proposal, scanned, objects, used[name], shared, asserted, problems)
        if problems or procedure is None:
            unadoptable[name] = _Unadoptable(
                declared if isinstance(declared, str) else name,
                Quoted(scanned["title"] or name),
                tuple(map(Quoted, problems)),
                frozenset(used[name]),
            )
            continue
        found[procedure.id] = procedure
        sidecars[procedure.id] = name
    for procedure in tuple(found.values()):
        if said := _unsettled(procedure, found):
            name = sidecars[procedure.id]
            del found[procedure.id]
            unadoptable[name] = _Unadoptable(
                procedure.id, Quoted(procedure.title or name), (Quoted(said),), frozenset(used[name])
            )
    return found, unadoptable


def _unsettled(procedure: AdoptedProcedure, found: Mapping[str, AdoptedProcedure]) -> str:
    """Why the read ``procedure`` names to settle it cannot, or "": it is a
    read of this catalog, on the same kinds of target, taking every revision
    its revision input takes, so HQ can ask it of the held run's machines at
    its revision. Whether the read requires one, and its words, are its own."""

    if procedure.settled_by is None:
        return ""
    read = found.get(procedure.settled_by)
    if read is None or read.effect != Effect.READ:
        return f"Its sidecar's settled_by names {procedure.settled_by}, which is no read of this commit."
    if read.target_kinds != procedure.target_kinds:
        return f"Its sidecar's settled_by names {read.id}, which runs on other kinds of target."
    given, taken = procedure.inputs.get(REVISION_INPUT), read.inputs.get(REVISION_INPUT)
    takes = ("type", "elements", "choices", "bound")
    if given is None or taken is None or taken.model_dump(include=set(takes)) != given.model_dump(include=set(takes)):
        return f"Its sidecar's settled_by names {read.id}, which does not take its {REVISION_INPUT} input."
    return ""


def _used(scanned: Mapping[str, Any], objects: Mapping[str, str]) -> set[str]:
    """The files a scanned playbook runs: its sidecar, the playbook, and its roles' files."""

    roles = [role["role"] for role in scanned["roles"]]
    return {scanned["sidecar"], scanned["playbook"], *_role_files(scanned["playbook"], roles, objects)}


def _procedure(
    proposal: SourceProposal,
    scanned: Mapping[str, Any],
    objects: Mapping[str, str],
    used: set[str],
    shared: Mapping[str, str],
    asserted: Mapping[str, tuple[str, ...]],
    problems: list[str],
) -> AdoptedProcedure | None:
    """The playbook ``scanned`` as HQ would adopt it, verified by its own
    post_tasks and those of the read that settles it (``asserted``), with
    each problem that keeps it from being adopted added to ``problems``."""

    try:
        declaration = ProcedureDeclaration.model_validate_json(json.dumps(scanned["declaration"]))
        settling = asserted.get(declaration.settled_by, ()) if declaration.settled_by else ()
        problems.extend(
            f"Its sidecar's readings names {kind}, which is no reading HQ declares."
            for kind in declaration.readings
            if wording(kind) is None
        )
        inputs = procedure_inputs(scanned, declaration.inputs, problems)
        procedure = AdoptedProcedure(
            **declaration.model_dump(include=set(ProcedureTerms.model_fields)),
            playbook=scanned["playbook"],
            title=scanned["title"],
            preconditions=tuple(scanned["preconditions"]),
            verification=(*scanned["verification"], *settling),
            inputs=inputs,
            revision=proposal.revision,
            files={path: objects[path] for path in sorted(used) if path in objects},
            shared=dict(shared),
        )
        manifest(procedure)
    except (ValidationError, ValueError) as exc:
        said = exc.errors(include_url=False)[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        problems.append(ended(capfirst(said)))
        return None
    return procedure


def _does(effect: Effect) -> str:
    """What a procedure of ``effect`` does, in the words its command's page says it with."""

    return lower_first(effect.label)


def titles(procedures: Mapping[str, AdoptedProcedure]) -> dict[str, str]:
    """Each playbook by the title you read it by, with its id beside a title
    two of them share, so the two stay apart."""

    return _apart({key: procedure.title for key, procedure in procedures.items()})


def _apart(titled: Mapping[str, str]) -> dict[str, str]:
    shared = Counter(titled.values())
    # A key is the author's too (a sidecar's id or path), so the pair is still their words.
    return {key: title if shared[title] == 1 else Quoted(f"{title} ({key})") for key, title in titled.items()}


def playbook_titles(digest: str | None = None) -> dict[str, str]:
    """Each adopted playbook's title by its id, the name a person reads it and
    its runs by, at ``digest`` (this process's generation when not given)."""

    return titles({procedure.id: procedure for procedure in procedures(digest)})


def _gains(before: AdoptedProcedure, after: AdoptedProcedure, named: str) -> list[str]:
    """How ``after``, read as ``named``, lets a run of the playbook do more than ``before`` did."""

    gained = []
    if before.hosts and (not after.hosts or set(after.hosts) - set(before.hosts)):
        gained.append(f"{named} can run on more machines.")
    if set(after.target_kinds) - set(before.target_kinds):
        gained.append(f"{named} can run on more types of record.")
    if after.effect.rank > before.effect.rank:
        gained.append(ended(f"{named} now {_does(after.effect)}, where before it {_does(before.effect)}"))
    if after.privilege and after.privilege != before.privilege:
        gained.append(ended(f"{named} gains {access_words(after.privilege)}"))
    # A read's files too: a read held only by its declaration could be made to
    # read anything its login can by a change no person looked at.
    if after.files != before.files:
        gained.append(f"{named} runs different files.")
    if before.shared != after.shared:
        gained.append(f"{named} can read changed files every playbook can read.")
    return gained


def _widens(
    before: Mapping[str, AdoptedProcedure], after: Mapping[str, AdoptedProcedure], keys: Iterable[str]
) -> list[str]:
    """Each way adopting the playbooks ``keys`` names, as ``after`` holds
    them, over ``before`` lets a run do more."""

    widens = []
    called = titles(after)
    for key in sorted(keys):
        procedure, was = after.get(key), before.get(key)
        if procedure is None:
            continue
        if was is None:
            if procedure.effect.changes or procedure.privilege is not None:
                widens.append(ended(f"{called[key]} is new, and it {_does(procedure.effect)}"))
            continue
        widens.extend(_gains(was, procedure, called[key]))
    return widens


def _summary(procedure: AdoptedProcedure) -> str:
    """Its Effect, Runs on and Access, in the Playbooks table's words."""

    where = ", ".join(procedure.hosts) or anywhere(procedure.target_kinds)
    return dotted(procedure.effect.label, where, access_words(procedure.privilege))


def _shown(procedure: AdoptedProcedure, named: str) -> dict[str, Any]:
    """A playbook as you compare it: ``named``, the name its group is headed
    by, and what it is at a glance, then each term in the words its command's
    page uses, each file by the start of its object id. Its playbook is among
    its files, so it is not said again."""

    return {
        SUMMARY: {"label": named, "value": _summary(procedure)},
        "effect": procedure.effect.label,
        "runs_on": list(procedure.hosts) or anywhere(procedure.target_kinds),
        "access": access_words(procedure.privilege),
        "timeout": timeout_words(procedure.timeout_seconds),
        "retry": RETRY_WORDS[procedure.retry],
        "recovery": list(procedure.recovery),
        "preconditions": list(procedure.preconditions),
        "verification": list(procedure.verification),
        "fixes": [found.title if (found := rule_for(rule)) else rule for rule in procedure.fixes],
        "investigates": [found.title if (found := rule_for(rule)) else rule for rule in procedure.investigates],
        "readings": [reading_words(kind) for kind in procedure.readings],
        "inputs": [field_words(name) for name in procedure.inputs],
        "files": {path: name[:SHOWN_OBJECT] for path, name in procedure.files.items()},
        "shared_files": {path: name[:SHOWN_OBJECT] for path, name in procedure.shared.items()},
    }


type Change = Literal["added", "changed", "removed"]


@dataclass(frozen=True, slots=True)
class PlaybookChange:
    """One playbook a scan changes against the catalog adopted now, waiting
    for a person: the key its adoption and rejection name it by, its title,
    how it changes, as the change table's Status says it, and each reason HQ
    can't adopt it."""

    key: str
    title: str
    change: Change
    problems: tuple[str, ...] = ()
    # The playbook as adopting it would make it; None for one removed or one HQ can't adopt.
    procedure: AdoptedProcedure | None = None


@dataclass(frozen=True, slots=True)
class AdoptionPlan:
    """What adopting one proposal would do: each playbook it changes against
    the catalog adopted now, how that compares, and every reason HQ will not
    adopt it."""

    proposal: SourceProposal
    procedures: dict[str, AdoptedProcedure]
    # The catalog adopted now, by playbook.
    before: dict[str, AdoptedProcedure]
    # Each change that waits: neither adopted nor rejected, ordered by title.
    changes: tuple[PlaybookChange, ...]
    widens: tuple[str, ...]
    compared: dict[str, Any]
    # Every reason, the commit's own first, then each waiting playbook's by its sidecar.
    refused: tuple[str, ...] = ()
    # The commit's own reasons, which no playbook's row of ``compared`` carries.
    unplaced: tuple[str, ...] = ()
    # Each waiting playbook HQ can't adopt, by the name its row of ``compared`` has.
    unadoptable: tuple[str, ...] = ()


def _digest(procedures: Iterable[AdoptedProcedure]) -> str:
    return digest({"procedures": [procedure.model_dump(mode="json") for procedure in procedures]})


def _commit_refusals(proposal: SourceProposal) -> list[str]:
    """Why HQ will not adopt the commit whatever its playbooks are."""

    refused = []
    if not proposal.verified or proposal.verification_reason != "valid":
        refused.append(f"GitHub did not verify the commit's signature ({proposal.verification_reason}).")
    return [*refused, *_carried(proposal)]


def _carried(proposal: SourceProposal) -> list[str]:
    """Why HQ will not adopt the commit for a file it carries."""

    return [
        ended(f"{path['path']} is {REFUSED_KINDS[path['kind']]}")
        for path in proposal.unpinned
        if path["kind"] in REFUSED_KINDS
    ]


def refusals(catalog: dict[str, Any]) -> tuple[str, ...]:
    """Every reason HQ would not adopt a commit a scan read (``catalog``, the
    bridge contract's ``SourceCatalog``), or each of its playbooks, with the
    catalog adopted now empty; all but its signature, which GitHub says of a
    commit only once it is pushed. A repository of playbooks checks its own
    commit with it before it merges (``tools/pipeline/check_playbooks.py``)."""

    proposal = automation_sources.proposal(catalog)
    _found, unadoptable = _procedures(proposal)
    return (
        *_carried(proposal),
        *(Quoted(f"{sidecar}: {problem}") for sidecar, found in unadoptable.items() for problem in found.problems),
    )


def _changes(
    before: Mapping[str, AdoptedProcedure],
    after: Mapping[str, AdoptedProcedure],
    unadoptable: Mapping[str, tuple[str, _Unadoptable]],
    called_before: Mapping[str, str],
    called_after: Mapping[str, str],
) -> list[PlaybookChange]:
    """Each playbook whose adopted version the scan changes: one new, one
    changed, or one it no longer holds, and one HQ can't adopt by the key it
    is compared by (``unadoptable``, each by its sidecar)."""

    changes = [
        PlaybookChange(key, called_after[key], "added" if key not in before else "changed", procedure=procedure)
        for key, procedure in after.items()
        if key not in before or not before[key].same(procedure)
    ]
    changes.extend(
        PlaybookChange(
            key,
            called_after[key],
            "changed" if key in before else "added",
            tuple(Quoted(f"{sidecar}: {problem}") for problem in found.problems),
        )
        for sidecar, (key, found) in unadoptable.items()
    )
    placed = {key for key, _found in unadoptable.values()}
    changes.extend(
        PlaybookChange(key, called_before[key], "removed") for key in before if key not in after and key not in placed
    )
    return sorted(changes, key=lambda change: (change.title, change.key))


def judged(proposal_id: int) -> AdoptionPlan:
    """What adopting the proposal would do and how it compares with the
    catalog adopted now, each playbook HQ can't adopt on its own row with
    its problems, and a playbook a person rejected left as adopted;
    RecordNotFound for a proposal HQ no longer holds."""

    proposal = SourceProposal.objects.filter(pk=proposal_id).first()
    if proposal is None:
        raise RecordNotFound("No scan result has that ID.")
    unplaced = _commit_refusals(proposal)
    procedures, unadoptable = _procedures(proposal)
    current = adopted()
    before = {row.id: row for row in _catalog(current.digest).procedures} if current else {}
    # A playbook HQ can't adopt is compared by its id where it declares one
    # no other row has, so it lines up with the playbook it would replace.
    keys, placed = set(procedures), {}
    for sidecar, found in unadoptable.items():
        placed[sidecar] = found.key if found.key not in keys else sidecar
        keys.add(placed[sidecar])
    called_before = titles(before)
    called_after = _apart(
        {
            **{key: procedure.title for key, procedure in procedures.items()},
            **{placed[sidecar]: found.title for sidecar, found in unadoptable.items()},
        }
    )
    rejected = set(proposal.rejected_playbooks)
    changes = tuple(
        change
        for change in _changes(
            before,
            procedures,
            {sidecar: (placed[sidecar], found) for sidecar, found in unadoptable.items()},
            called_before,
            called_after,
        )
        if change.key not in rejected
    )
    # What is compared leaves out a rejected playbook, on both sides.
    shown_before = {key: _shown(row, called_before[key]) for key, row in before.items() if key not in rejected}
    shown_after = {key: _shown(row, called_after[key]) for key, row in procedures.items() if key not in rejected}
    for sidecar, found in unadoptable.items():
        key = placed[sidecar]
        if key in rejected:
            continue
        # The playbook it would replace, as it stands, so only its problems move.
        standing = _shown(before[key], called_after[key]) if key in before else {}
        shown_after[key] = {
            **standing,
            SUMMARY: {"label": called_after[key], "value": CANT_ADOPT},
            "problems": list(found.problems),
        }
    waiting = {change.key for change in changes}
    return AdoptionPlan(
        proposal=proposal,
        procedures=procedures,
        before=before,
        changes=changes,
        widens=tuple(_widens(before, procedures, waiting)),
        compared={
            "before": {
                "commit": current.revision[:SHOWN_OBJECT] if current else "",
                "committer": current.committer if current else "",
                "procedures": dict(sorted(shown_before.items())),
            },
            "after": {
                "commit": proposal.revision[:SHOWN_OBJECT],
                "committer": proposal.committer,
                "procedures": dict(sorted(shown_after.items())),
            },
        },
        refused=(
            *unplaced,
            *(
                Quoted(f"{sidecar}: {problem}")
                for sidecar, found in unadoptable.items()
                if placed[sidecar] in waiting
                for problem in found.problems
            ),
        ),
        unplaced=tuple(unplaced),
        unadoptable=tuple(called_after[placed[sidecar]] for sidecar in unadoptable if placed[sidecar] in waiting),
    )


def plan(proposal_id: int, playbook: str = "") -> AdoptionPlan:
    """What adopting the proposal would do, or the refusal that lists every
    reason HQ will not adopt it: of the commit, then of ``playbook`` when one
    is named, else of every playbook whose change waits while none of them
    can be adopted."""

    planned = judged(proposal_id)
    named = next((change for change in planned.changes if change.key == playbook), None)
    if playbook:
        refused = (*planned.unplaced, *(named.problems if named else ()))
    else:
        adoptable = any(not change.problems for change in planned.changes)
        refused = planned.unplaced if adoptable or not planned.changes else planned.refused
    if refused:
        raise PolicyError(
            "This can't be adopted; details.refused lists why. Fix it in the repository; HQ scans the push.",
            details={"refused": list(refused)},
        )
    return planned


# ----------------------------------------------------------------- adopting


class Adopted(TypedDict):
    """What an adoption did: whether the adopted playbooks changed, and what runs may now do."""

    ok: Literal[True]
    adopted: Annotated[bool, Describe("False when nothing it names waited to be adopted.")]
    digest: Annotated[str, Describe("The digest that names the adopted playbooks.")]
    widened: Annotated[list[str], Describe("What runs may now do that they could not before.")]
    message: CallMessage


# A playbook as a change of a scan names it: its id, or its sidecar where two declare one id.
PlaybookKey = Annotated[RequiredText, Strict(), Field(max_length=200, title="Playbook")]

# The scan result a change of a scan is adopted or rejected from.
type ScanResult = Annotated[RecordId, Strict(), Describe("The scan result's ID.")]


@dataclass(frozen=True, slots=True)
class AdoptProposal:
    """The scan result to adopt, and the one playbook to adopt from it."""

    proposal: Annotated[ScanResult, Field(title="Scan result")]
    playbook: Annotated[
        PlaybookKey | None,
        Describe(
            "The playbook whose change to adopt, by its id; left out, every change that waits and HQ can adopt "
            "is adopted."
        ),
    ] = None


def _everyone(_principal: Principal) -> bool:
    return True


def _decided(planned: AdoptionPlan, playbook: str | None) -> tuple[PlaybookChange, ...]:
    """The changes a decision on ``playbook`` takes, or every one that waits
    when it names none; RecordNotFound for a playbook the scan neither holds
    nor removes, and a refusal for one a person rejected."""

    if playbook is None:
        return planned.changes
    chosen = tuple(change for change in planned.changes if change.key == playbook)
    if chosen:
        return chosen
    if playbook in planned.proposal.rejected_playbooks:
        raise PolicyError("That playbook's change was rejected.")
    if playbook not in planned.procedures and playbook not in planned.before:
        raise RecordNotFound("No playbook of this scan result has that id.")
    return ()


@transaction.atomic
def adopt(command: AdoptProposal, *, principal: Principal) -> Adopted:
    """Adopt the change the proposal makes to one playbook, or every change
    that waits and HQ can adopt, so each runs from the proposal's commit;
    every other playbook stays as it was adopted, and one HQ can't adopt
    still waits."""

    planned = plan(command.proposal, command.playbook or "")
    chosen = tuple(change for change in _decided(planned, command.playbook) if not change.problems)
    current = adopted()
    if not chosen:
        return {
            "ok": True,
            "adopted": False,
            "digest": current.digest if current else "",
            "widened": [],
            "message": "Already adopted.",
        }
    catalog = dict(planned.before)
    for change in chosen:
        if change.procedure is None:
            catalog.pop(change.key, None)
        else:
            catalog[change.key] = change.procedure
    ordered = [catalog[key] for key in sorted(catalog)]
    proposal = planned.proposal
    row = AdoptedCatalog.objects.create(
        proposal=proposal,
        branch=proposal.branch,
        revision=proposal.revision,
        committer=proposal.committer,
        procedures=[procedure.model_dump(mode="json") for procedure in ordered],
        digest=_digest(ordered),
    )
    # Every caller's commands moved: this process builds its own anew once
    # the row commits, every other at its next request (``generation``), and
    # a stream is told.
    transaction.on_commit(catch_up, robust=True)
    agent_registry.tools_changed(_everyone)
    widened = _widens(planned.before, planned.procedures, (change.key for change in chosen))
    return {"ok": True, "adopted": True, "digest": row.digest, "widened": widened, "message": "Adopted."}


class Rejected(TypedDict):
    """What a rejection did: whether what it names waited to be decided until now."""

    ok: Literal[True]
    rejected: Annotated[bool, Describe("False when what it names was already rejected.")]
    message: CallMessage


@dataclass(frozen=True, slots=True)
class RejectProposal:
    """The scan result to reject, and the one playbook to reject from it."""

    proposal: Annotated[ScanResult, Field(title="Scan result")]
    playbook: Annotated[
        PlaybookKey | None,
        Describe(
            "The playbook whose change to reject, by its id, so it stays as adopted; "
            "left out, the whole scan result is rejected."
        ),
    ] = None


@transaction.atomic
def reject(command: RejectProposal, *, principal: Principal) -> Rejected:
    """Set aside the change a scan result makes to one playbook, which stays
    as adopted, or the whole scan result, so nothing of it waits to be
    adopted until the next scan reads another commit."""

    proposal = SourceProposal.objects.select_for_update().filter(pk=command.proposal).first()
    if proposal is None:
        raise RecordNotFound("No scan result has that ID.")
    if proposal.rejected_at is not None or command.playbook in proposal.rejected_playbooks:
        return {"ok": True, "rejected": False, "message": "Already rejected."}
    chosen = _decided(judged(proposal.pk), command.playbook)
    if not chosen:
        raise PolicyError("Nothing in this scan result waits to be adopted, so there is nothing to reject.")
    if command.playbook is not None:
        proposal.rejected_playbooks = [*proposal.rejected_playbooks, command.playbook]
        proposal.save(update_fields=("rejected_playbooks", "updated_at"))
    else:
        proposal.rejected_at = timezone.now()
        proposal.save(update_fields=("rejected_at", "updated_at"))
    return {"ok": True, "rejected": True, "message": "Rejected."}


def prune_proposals() -> int:
    """Delete proposals older than the routine audit window that nothing
    still needs: never the newest or the one adopted now. Returns how many."""

    cutoff = timezone.now() - timedelta(days=int(settings.SEVERINO_AUDIT_ROUTINE_DAYS))
    keep = {
        SourceProposal.objects.order_by("-pk").values_list("pk", flat=True).first(),
        AdoptedCatalog.objects.values_list("proposal_id", flat=True).first(),
    } - {None}
    stale = SourceProposal.objects.filter(created_at__lt=cutoff).exclude(pk__in=keep)
    deleted = 0
    for proposal in stale.iterator():
        with transaction.atomic():
            proposal.delete()
        deleted += 1
    return deleted


def capability_specs() -> tuple[CapabilitySpec, ...]:
    """The automation source's commands: Adopt and Reject, which decide on what a scan proposes."""

    return (
        CapabilitySpec(
            ADOPT,
            "Adopt what a scanned commit changes in the playbooks HQ runs: one playbook's change, or every "
            "change that waits and HQ can adopt. A commit GitHub did not verify, or one carrying an ansible.cfg "
            "or a plugins directory, is refused. Only an operator adopts, and never through an agent.",
            Effect.INFRASTRUCTURE_CHANGE,
            # Held only at the operator's own interfaces (security.HELD_ONLY_AT).
            Capability.ADOPT_PLAYBOOKS,
            AdoptProposal,
            adopt,
            execution_notes=(
                "Refuse a commit GitHub did not verify or one carrying an ansible.cfg or a plugins directory.",
                "Refuse a playbook HQ can't run; every change at once leaves it waiting.",
                "Compare it, playbook by playbook, with the adopted playbooks.",
                "Each playbook adopted runs from this commit from now on; every other stays as adopted.",
                "A run of a changed playbook that waits for approval can no longer be approved.",
            ),
            label="Adopt the playbooks",
            verb="Adopt",
            variants=(("all", "Adopt all"), (ADOPTABLE, "Adopt all HQ can")),
            home=automation_sources.PLAYBOOKS,
        ),
        CapabilitySpec(
            REJECT,
            "Reject what a scanned commit changes: one playbook's change, which stays as adopted, or the whole "
            "commit, so nothing waits to be adopted until a scan reads another. Only an operator rejects, "
            "and never through an agent.",
            Effect.REMOTE_WRITE,
            Capability.ADOPT_PLAYBOOKS,
            RejectProposal,
            reject,
            execution_notes=(
                "Refuse a scan result with nothing waiting to be adopted.",
                "Mark the playbook's change or the scan result rejected, recorded under your name.",
            ),
            label="Reject a scanned commit",
            verb="Reject",
            variants=(("all", "Reject all"),),
            home=automation_sources.PLAYBOOKS,
        ),
    )
