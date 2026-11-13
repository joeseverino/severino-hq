"""Adopting a proposal: one scanned commit of the automation source becomes
the catalog HQ runs procedures from, once a person agrees.

A Refresh proposes (``automation_sources``); this adopts. The newest
``AdoptedCatalog`` row is the one declaration every procedure's command, its
input type and the spec a held run is measured against are built from
(``automation_runs``), and its digest is the generation each process checks
its commands against, so an adoption reaches every process at its next
request without a restart. How a commit is judged, and what holds an
adoption for a person: docs/Architecture.md, "Adopting the playbooks".

Security properties:

- Only a commit GitHub verified as signed is adopted. The scan read that with
  the worker's token (``hq_runner.catalog.github_verify``); this process holds
  no token and asks nothing of GitHub, so it trusts the verification the
  scan recorded and nothing else.
- A commit carrying an ``ansible.cfg`` or a directory Ansible loads code from
  (``unpinned``, kinds ``config`` and ``plugins``) is refused, since a run
  would load what no reviewer of a procedure reads.
- Adoption is a person's act. A caller without a person behind it waits for
  one; an agent's own rule may let it adopt a narrow change without asking,
  and a widening (a write gains its ground, a new write, a changed file a
  write runs, a file every procedure may read) waits whatever its rule says.
- Each input is closed: a text input has its choices or a sidecar's pattern
  and length, and a free-form type is refused, so a run's parameters can
  name no execution field (``automation_manifest._closed``).
"""

import hashlib
import json
import re
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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    create_model,
)

from hq.domains.control_plane.models import AdoptedCatalog, ApprovalRequest, SourceProposal
lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND
lazy from hq.platform.core.audit import audit_operation

from . import automation_sources
from .approvals import COMPARED, WARNINGS, ApprovalSubject, measured_by, waits_for_a_person
from .automation_manifest import PLAYBOOK, OperationManifest, ProcedureTerms, Statement
from .automation_sources import SOURCE_KEY, InputBound, ProcedureDeclaration
from .command_fields import Describe
from .integration_specs import CapabilitySpec
from .record_errors import PolicyError, RecordNotFound
from .security import Capability, Principal
lazy from . import agent_registry
lazy from .approval_review import SUMMARY
lazy from .automation_runs import RETRY_WORDS
lazy from .entity_links import kind_label
lazy from .labels import effect_label, lower_first, plural
lazy from .ui import ended

ADOPT = "automation.source.adopt"
# The names an input may have, as a sidecar's ``inputs`` keys them.
INPUT_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
# The kinds of a path a proposal may carry and still be adopted. A
# configuration or a plugin directory is code no procedure's review reads.
REFUSED_KINDS = {
    "config": "an ansible.cfg, which changes how every playbook runs",
    "plugins": "a plugins directory, whose code runs with every playbook",
}
# Where a role's own files sit, beside a playbook or at the project's root.
ROLES = "roles"
# The file that names the collections the worker's image is built with.
REQUIREMENTS = "requirements.yml"
# How a change's effect ranks, so a raise is seen as one.
EFFECT_RANK = {"read": 0, "infrastructure_change": 1, "destructive": 2}
# The most characters of an object id a comparison shows: enough to tell two apart.
SHOWN_OBJECT = 12

# Ansible's own types, as an argument spec names them, that a closed input
# may be; ``type`` left out is a string, as Ansible reads it. The free-form
# ones (``dict``, ``raw``, ``json``, ``jsonarg``) and the ones a run has no
# use taking (``bits``, ``bytes``, ``sid``) are refused.
SCALARS = {"str": StrictStr, "path": StrictStr, "int": StrictInt, "float": StrictFloat, "bool": StrictBool}
TEXT = frozenset({"str", "path"})
type Scalar = Literal["str", "path", "int", "float", "bool"]


class AdoptedInput(BaseModel):
    """One input of a playbook, as adopted: its argument spec's terms, and
    the sidecar's limit on a text input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["str", "path", "int", "float", "bool", "list"]
    elements: Scalar | None = None
    required: bool = False
    default: (
        StrictStr | StrictInt | StrictFloat | StrictBool | tuple[StrictStr | StrictInt | StrictFloat, ...] | None
    ) = None
    choices: tuple[StrictStr | StrictInt | StrictFloat, ...] = ()
    description: Annotated[str, StringConstraints(max_length=1000)] = ""
    bound: InputBound | None = None


class AdoptedProcedure(ProcedureTerms):
    """One procedure of an adopted catalog: what its sidecar declares, what
    Ansible says of it, and the files it runs, each by its object id."""

    playbook: Annotated[str, StringConstraints(pattern=PLAYBOOK)]
    title: Annotated[str, StringConstraints(max_length=200)]
    preconditions: tuple[Statement, ...]
    verification: tuple[Statement, ...]
    inputs: dict[Annotated[str, StringConstraints(pattern=INPUT_NAME.pattern)], AdoptedInput]
    files: dict[str, str]


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
    """The catalog HQ runs from now, or None before any adoption."""

    return AdoptedCatalog.objects.first()


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
    # As JSON, which the row is stored as: a list is a strict tuple there.
    rows = tuple(AdoptedProcedure.model_validate_json(json.dumps(row)) for row in catalog.procedures)
    return _Known(digest, rows, tuple(manifest(row, catalog.revision) for row in rows))


def manifest(procedure: AdoptedProcedure, revision: str) -> OperationManifest:
    """The manifest HQ runs ``procedure`` from, at the adopted ``revision``."""

    return OperationManifest(
        **procedure.model_dump(include=set(ProcedureTerms.model_fields)),
        revision=revision,
        playbook=procedure.playbook,
        input_type=input_type(procedure.id, procedure.inputs),
        preconditions=procedure.preconditions,
        verification=procedure.verification,
    )


def _scalar(kind: str, choices: tuple[Any, ...], bound: InputBound | None) -> Any:
    if choices:
        return Literal[choices]
    if kind in TEXT:
        if bound is None:
            raise ValueError("a text input needs its choices or a bound")
        return Annotated[StrictStr, StringConstraints(pattern=bound.pattern, max_length=bound.max_length)]
    return SCALARS[kind]


def input_type(operation_id: str, inputs: Mapping[str, AdoptedInput]) -> type[BaseModel]:
    """The closed model a procedure's parameters are validated with, from its
    inputs as adopted: each by its Ansible type, its choices or its bound."""

    fields: dict[str, Any] = {}
    for name, spec in inputs.items():
        if spec.type == "list":
            if spec.elements is None:
                raise ValueError(f"the list input {name} names no type for its elements")
            annotation: Any = tuple[_scalar(spec.elements, spec.choices, spec.bound), ...]
        else:
            annotation = _scalar(spec.type, spec.choices, spec.bound)
        described = Annotated[annotation, Describe(spec.description)] if spec.description else annotation
        if spec.required:
            fields[name] = (described, ...)
        elif spec.default is None:
            fields[name] = (described | None, None)
        else:
            fields[name] = (described, Field(default=spec.default, validate_default=True))
    title = "".join(part.title() for part in operation_id.replace(".", "_").split("_"))
    model: type[BaseModel] = create_model(
        f"{title}Inputs", __config__=ConfigDict(extra="forbid", frozen=True), __module__=__name__, **fields
    )
    return model


# ------------------------------------------------------------ judging a commit


def _description(value: Any) -> str:
    text = " ".join(value) if isinstance(value, list) else value if isinstance(value, str) else ""
    return text[:1000]


def _inputs(found: Mapping[str, Any], bounds: Mapping[str, InputBound], refused: list[str], name: str) -> dict:
    """The inputs every role of one procedure declares, each once, bounded as its sidecar says."""

    options: dict[str, Mapping[str, Any]] = {}
    for role in found["roles"]:
        spec = role.get("argument_spec") or {}
        for option, terms in (spec.get("options") or {}).items():
            if option in options and options[option] != terms:
                refused.append(f"{name}: two of its roles declare the input {option} differently.")
            options.setdefault(option, terms if isinstance(terms, Mapping) else {})
    refused.extend(
        f"{name}: its sidecar limits the input {unknown}, which none of its roles takes."
        for unknown in sorted(set(bounds) - set(options))
    )
    inputs: dict[str, AdoptedInput] = {}
    for option, terms in sorted(options.items()):
        problem = _input(option, terms, bounds.get(option), inputs)
        if problem:
            refused.append(f"{name}: {problem}")
    return inputs


def _input(option: str, terms: Mapping[str, Any], bound: InputBound | None, inputs: dict[str, AdoptedInput]) -> str:
    """Adds ``option`` to ``inputs`` as Ansible's terms and its sidecar's
    ``bound`` say, or answers why it cannot run."""

    kind = terms.get("type", "str")
    if not INPUT_NAME.fullmatch(option):
        return f"the input name {option} isn't allowed; use lowercase letters, digits and underscores."
    if kind not in (*SCALARS, "list") or terms.get("elements") not in (None, *SCALARS):
        return f"the input {option} takes {kind}, a free-form type; name each value it allows."
    try:
        adopted = AdoptedInput.model_validate_json(
            json.dumps(
                {
                    "type": kind,
                    "elements": terms.get("elements"),
                    "required": bool(terms.get("required", False)),
                    "default": terms.get("default"),
                    "choices": terms.get("choices") or [],
                    "description": _description(terms.get("description")),
                    "bound": bound.model_dump() if bound is not None else None,
                }
            )
        )
    except ValidationError as exc:
        return f"the input {option} cannot be read ({exc.errors(include_url=False)[0]['msg']})."
    inputs[option] = adopted
    text = adopted.elements if kind == "list" else kind
    if text in TEXT and not adopted.choices and bound is None:
        return f"the text input {option} needs its choices, or a pattern and length in its sidecar."
    return ""


def _role_files(playbook: str, roles: Iterable[str], paths: Iterable[str]) -> set[str]:
    """The files of the roles a playbook lists, wherever Ansible finds them."""

    places = [f"{ROLES}/{role}/" for role in roles]
    beside = PurePosixPath(playbook).parent
    if str(beside) != ".":
        places += [f"{beside}/{ROLES}/{role}/" for role in roles]
    return {path for path in paths if any(path.startswith(place) for place in places)}


def _procedures(proposal: SourceProposal, refused: list[str]) -> dict[str, AdoptedProcedure]:
    objects = {entry["path"]: entry["object"] for entry in proposal.files}
    found: dict[str, AdoptedProcedure] = {}
    for scanned in proposal.procedures:
        name = scanned["sidecar"]
        if scanned["problems"]:
            refused.extend(f"{name}: {problem}" for problem in scanned["problems"])
            continue
        roles = [role["role"] for role in scanned["roles"]]
        used = {name, scanned["playbook"], *_role_files(scanned["playbook"], roles, objects)}
        try:
            declaration = ProcedureDeclaration.model_validate_json(json.dumps(scanned["declaration"]))
            inputs = _inputs(scanned, declaration.inputs, refused, name)
            procedure = AdoptedProcedure(
                **declaration.model_dump(include=set(ProcedureTerms.model_fields)),
                playbook=scanned["playbook"],
                title=scanned["title"],
                preconditions=tuple(scanned["preconditions"]),
                verification=tuple(scanned["verification"]),
                inputs=inputs,
                files={path: objects[path] for path in sorted(used) if path in objects},
            )
            manifest(procedure, proposal.revision)
        except (ValidationError, ValueError) as exc:
            said = exc.errors(include_url=False)[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            refused.append(f"{name}: {said}")
            continue
        found[procedure.id] = procedure
    return found


def _shared(files: Mapping[str, str], procedures: Iterable[AdoptedProcedure]) -> dict[str, str]:
    """The files no procedure is known to run, which any of them may read."""

    used = {path for procedure in procedures for path in procedure.files}
    return {path: name for path, name in files.items() if path not in used}


def _does(effect: str) -> str:
    """What a procedure of ``effect`` does, in the words its command's page says it with."""

    return lower_first(effect_label(effect))


def titles(procedures: Mapping[str, AdoptedProcedure]) -> dict[str, str]:
    """Each playbook by the title you read it by, with its id beside a title
    two of them share, so the two stay apart."""

    shared = Counter(procedure.title for procedure in procedures.values())
    return {
        key: procedure.title if shared[procedure.title] == 1 else f"{procedure.title} ({key})"
        for key, procedure in procedures.items()
    }


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
    if EFFECT_RANK[after.effect] > EFFECT_RANK[before.effect]:
        gained.append(ended(f"{named} now {_does(after.effect)}, where before it {_does(before.effect)}"))
    if after.privilege and after.privilege != before.privilege:
        gained.append(ended(f"{named} gains the privilege {after.privilege}"))
    if after.effect != "read" and after.files != before.files:
        gained.append(f"{named} runs different files.")
    return gained


def _widens(
    before: Mapping[str, AdoptedProcedure],
    after: Mapping[str, AdoptedProcedure],
    shared_before: Mapping[str, str],
    shared_after: Mapping[str, str],
) -> list[str]:
    """Each way adopting ``after`` over ``before`` lets a run do more."""

    widens = []
    called = titles(after)
    for key, procedure in sorted(after.items()):
        was = before.get(key)
        if was is None:
            if procedure.effect != "read" or procedure.privilege is not None:
                widens.append(ended(f"{called[key]} is new, and it {_does(procedure.effect)}"))
            continue
        widens.extend(_gains(was, procedure, called[key]))
    for path in sorted(set(shared_before) | set(shared_after)):
        if shared_before.get(path) == shared_after.get(path):
            continue
        if PurePosixPath(path).name == REQUIREMENTS:
            widens.append(f"{path} changed. It sets which Ansible collections playbooks can use.")
        else:
            widens.append(f"{path} changed. Every playbook can read it.")
    return widens


def anywhere(procedure: ProcedureTerms) -> str:
    """Where a playbook that names no machine may run: "Any of your machines"."""

    kinds = " or ".join(plural(lower_first(kind_label(kind))) for kind in procedure.target_kinds)
    return f"Any of your {kinds}"


def _summary(procedure: AdoptedProcedure) -> str:
    where = ", ".join(procedure.hosts) if procedure.hosts else lower_first(anywhere(procedure))
    privilege = f"with the privilege {procedure.privilege}" if procedure.privilege else "with no added privilege"
    return ended(f"{effect_label(procedure.effect)}, on {where}, {privilege}")


def _shown(procedure: AdoptedProcedure, named: str) -> dict[str, Any]:
    """A playbook as you compare it: ``named``, the name its group is headed
    by, and what it is at a glance, then each term in the words its command's
    page uses, each file by the start of its object id. Its playbook is among
    its files, so it is not said again."""

    return {
        SUMMARY: {"label": named, "value": _summary(procedure)},
        "title": procedure.title,
        "effect": effect_label(procedure.effect),
        "runs_on": list(procedure.hosts) or anywhere(procedure),
        "privilege": procedure.privilege or "None",
        "timeout": f"{procedure.timeout_seconds} seconds",
        "retry": RETRY_WORDS[procedure.retry],
        "recovery": list(procedure.recovery),
        "preconditions": list(procedure.preconditions),
        "verification": list(procedure.verification),
        "inputs": {
            name: spec.model_dump(mode="json", exclude_defaults=True) for name, spec in procedure.inputs.items()
        },
        "files": {path: name[:SHOWN_OBJECT] for path, name in procedure.files.items()},
    }


@dataclass(frozen=True, slots=True)
class AdoptionPlan:
    """What adopting one proposal would do: the catalog it writes, and how it moves the one adopted now."""

    proposal: SourceProposal
    procedures: dict[str, AdoptedProcedure]
    files: dict[str, str]
    digest: str
    current: str
    widens: tuple[str, ...]
    compared: dict[str, Any]


def _digest(revision: str, procedures: Iterable[AdoptedProcedure]) -> str:
    material = json.dumps(
        {"revision": revision, "procedures": [procedure.model_dump(mode="json") for procedure in procedures]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def plan(proposal_id: int) -> AdoptionPlan:
    """What adopting the proposal would do, or the refusal that lists every
    reason HQ will not adopt it."""

    proposal = SourceProposal.objects.filter(pk=proposal_id).first()
    if proposal is None:
        raise RecordNotFound("No scan result has that ID. Refresh to scan again.")
    refused: list[str] = []
    if not proposal.verified or proposal.verification_reason != "valid":
        refused.append(f"GitHub did not verify the commit's signature ({proposal.verification_reason}).")
    refused.extend(
        ended(f"{path['path']} is {REFUSED_KINDS[path['kind']]}")
        for path in proposal.unpinned
        if path["kind"] in REFUSED_KINDS
    )
    procedures = _procedures(proposal, refused)
    if refused:
        raise PolicyError(
            "This commit can't be adopted; details.refused lists why. Fix it in the repository, then Refresh.",
            details={"refused": refused},
        )
    files = {entry["path"]: entry["object"] for entry in proposal.files}
    current = adopted()
    before = {row.id: row for row in _catalog(current.digest).procedures} if current else {}
    files_before = {entry["path"]: entry["object"] for entry in current.files} if current else {}
    shared_before, shared_after = _shared(files_before, before.values()), _shared(files, procedures.values())
    ordered = [procedures[named] for named in sorted(procedures)]
    called_before, called_after = titles(before), titles(procedures)
    return AdoptionPlan(
        proposal=proposal,
        procedures=procedures,
        files=files,
        digest=_digest(proposal.revision, ordered),
        current=current.digest if current else "",
        widens=tuple(_widens(before, procedures, shared_before, shared_after)),
        compared={
            "before": {
                "commit": current.revision[:SHOWN_OBJECT] if current else "",
                "committer": current.committer if current else "",
                "procedures": {key: _shown(row, called_before[key]) for key, row in sorted(before.items())},
                "shared files": {path: name[:SHOWN_OBJECT] for path, name in sorted(shared_before.items())},
            },
            "after": {
                "commit": proposal.revision[:SHOWN_OBJECT],
                "committer": proposal.committer,
                "procedures": {key: _shown(row, called_after[key]) for key, row in sorted(procedures.items())},
                "shared files": {path: name[:SHOWN_OBJECT] for path, name in sorted(shared_after.items())},
            },
        },
    )


# ----------------------------------------------------------------- adopting


class Adopted(TypedDict):
    """What an adoption did: whether the adopted playbooks changed, and what runs may now do."""

    ok: Literal[True]
    adopted: Annotated[bool, Describe("False when this scan result was already adopted.")]
    digest: Annotated[str, Describe("The digest that names the adopted playbooks.")]
    widened: Annotated[list[str], Describe("What runs may now do that they could not before.")]


class AdoptProposal(BaseModel):
    """The scan result to adopt, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: Annotated[StrictInt, Field(ge=1, title="Scan result"), Describe("The scan result's ID.")]
    reason: Annotated[StrictStr, Field(max_length=300), Describe("Why it is adopted, kept with the adoption.")] = ""


def subject(command: AdoptProposal) -> ApprovalSubject:
    """What a held adoption is measured against: the proposal, the catalog
    adopted now, and the comparison a person decides on, so another adoption
    while it waits makes it stale."""

    planned = plan(command.proposal)
    proposal = planned.proposal
    return ApprovalSubject(
        SOURCE_KIND,
        SOURCE_KEY,
        {
            "proposal": proposal.pk,
            "revision": proposal.revision,
            "committer": proposal.committer,
            "verification": proposal.verification_reason,
            "adopted": planned.current,
            WARNINGS: list(planned.widens),
            COMPARED: planned.compared,
        },
    )


def widens(command: AdoptProposal) -> bool:
    """Whether adopting the proposal lets a run do more than it may now: a
    change no standing rule consents to. A proposal HQ cannot read counts as
    one, and its subject then refuses it."""

    try:
        return bool(plan(command.proposal).widens)
    except RecordNotFound, PolicyError:
        return True


def _everyone(_principal: Principal) -> bool:
    return True


@waits_for_a_person(widens)
@measured_by(subject)
@transaction.atomic
def adopt(command: AdoptProposal, *, principal: Principal) -> Adopted:
    """Make the proposal the catalog HQ runs from, unless it already is."""

    with audit_operation(operation=ADOPT, principal=principal):
        planned = plan(command.proposal)
        if planned.digest == planned.current:
            return {"ok": True, "adopted": False, "digest": planned.digest, "widened": []}
        proposal = planned.proposal
        catalog = AdoptedCatalog.objects.create(
            proposal=proposal,
            branch=proposal.branch,
            revision=proposal.revision,
            committer=proposal.committer,
            procedures=[planned.procedures[named].model_dump(mode="json") for named in sorted(planned.procedures)],
            files=[{"path": path, "object": name} for path, name in sorted(planned.files.items())],
            digest=planned.digest,
        )
    # Every caller's commands moved: this process builds its own anew once
    # the row commits, every other at its next request (``generation``), and
    # a stream is told.
    transaction.on_commit(catch_up, robust=True)
    agent_registry.tools_changed(_everyone)
    return {"ok": True, "adopted": True, "digest": catalog.digest, "widened": list(planned.widens)}


def prune_proposals() -> int:
    """Delete proposals older than the routine audit window that nothing
    still needs: never the newest, the one adopted now, or one a request
    waiting for a person names. Returns how many."""

    cutoff = timezone.now() - timedelta(days=int(settings.SEVERINO_AUDIT_ROUTINE_DAYS))
    keep = {
        SourceProposal.objects.order_by("-pk").values_list("pk", flat=True).first(),
        AdoptedCatalog.objects.values_list("proposal_id", flat=True).first(),
        *(
            held.payload.get("proposal")
            for held in ApprovalRequest.objects.filter(capability=ADOPT, state=ApprovalRequest.State.PENDING)
        ),
    } - {None}
    stale = SourceProposal.objects.filter(created_at__lt=cutoff).exclude(pk__in=keep)
    deleted = 0
    for proposal in stale.iterator():
        with transaction.atomic():
            proposal.delete()
        deleted += 1
    return deleted


def capability_specs() -> tuple[CapabilitySpec, ...]:
    """The automation source's commands: Refresh, which proposes, and Adopt."""

    return (
        *automation_sources.capability_specs(),
        CapabilitySpec(
            ADOPT,
            "Adopt a scanned commit as the playbooks HQ runs. A commit GitHub did not verify, or one "
            "carrying an ansible.cfg or a plugins directory, is refused.",
            "infrastructure_change",
            Capability.MANAGE_INFRASTRUCTURE,
            AdoptProposal,
            adopt,
            execution_notes=(
                "Refuse a commit GitHub did not verify or one carrying an ansible.cfg or a plugins directory.",
                "Refuse a commit with a playbook HQ can't run.",
                "Compare it, playbook by playbook, with the adopted playbooks.",
                "Every playbook run from now on uses this commit.",
                "A run of a changed playbook that waits for approval can no longer be approved.",
            ),
            label="Adopt the playbooks",
            verb="Adopt",
            wants="adopt the playbooks of a commit from your repository",
            home=automation_sources.PLAYBOOKS,
        ),
    )
