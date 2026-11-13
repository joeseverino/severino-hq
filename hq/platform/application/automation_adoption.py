"""Adopting a proposal: what one scanned commit of the automation source
changes becomes the catalog HQ runs procedures from, playbook by playbook or
all at once, once a person agrees.

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
- Adoption is a person's act. Adopting and rejecting take
  ``adopt_playbooks``, held only at the operator's own interfaces
  (``security.HELD_ONLY_AT``): an agent may refresh, and its adoption is
  refused, never held for approval.
- Each procedure runs at the commit it was adopted from, which pins every
  file a run checks out, so a file a newer commit changes (a role two
  playbooks share) reaches only a playbook adopted from that commit.
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

from hq.domains.control_plane.models import AdoptedCatalog, SourceProposal
lazy from hq.platform.core.audit import audit_operation

from . import automation_sources
from .automation_manifest import PLAYBOOK, REVISION, OperationManifest, ProcedureTerms, Statement
from .automation_sources import InputBound, ProcedureDeclaration
from .command_fields import Describe
from .integration_specs import CapabilitySpec
from .record_errors import PolicyError, RecordNotFound
from .security import Capability, Principal
lazy from . import agent_registry
lazy from .approval_review import SUMMARY
lazy from .automation_runs import RETRY_WORDS, access_words, anywhere, timeout_words
lazy from .findings import rule_for
lazy from .labels import dotted, effect_label, lower_first
lazy from .payload_words import field_words
lazy from .ui import ended

ADOPT = "automation.source.adopt"
REJECT = "automation.source.reject"
# What a playbook HQ can't adopt is, at a glance, on its row of a comparison.
CANT_ADOPT = "HQ can't adopt it"
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
    Ansible says of it, the commit it runs at, and the files it runs and the
    ones of that commit no playbook runs (``shared``), each by its object id."""

    playbook: Annotated[str, StringConstraints(pattern=PLAYBOOK)]
    title: Annotated[str, StringConstraints(max_length=200)]
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
    return _Known(digest, rows, tuple(manifest(row) for row in rows))


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


def _inputs(found: Mapping[str, Any], bounds: Mapping[str, InputBound], refused: list[str]) -> dict:
    """The inputs every role of one procedure declares, each once, bounded as
    its sidecar says, with each problem among them added to ``refused``."""

    options: dict[str, Mapping[str, Any]] = {}
    for role in found["roles"]:
        spec = role.get("argument_spec") or {}
        for option, terms in (spec.get("options") or {}).items():
            if option in options and options[option] != terms:
                refused.append(f"Two of its roles declare the input {option} differently.")
            options.setdefault(option, terms if isinstance(terms, Mapping) else {})
    refused.extend(
        f"Its sidecar limits the input {unknown}, which none of its roles takes."
        for unknown in sorted(set(bounds) - set(options))
    )
    inputs: dict[str, AdoptedInput] = {}
    for option, terms in sorted(options.items()):
        problem = _input(option, terms, bounds.get(option), inputs)
        if problem:
            refused.append(problem)
    return inputs


def _input(option: str, terms: Mapping[str, Any], bound: InputBound | None, inputs: dict[str, AdoptedInput]) -> str:
    """Adds ``option`` to ``inputs`` as Ansible's terms and its sidecar's
    ``bound`` say, or answers why it cannot run."""

    kind = terms.get("type", "str")
    if not INPUT_NAME.fullmatch(option):
        return f"The input name {option} isn't allowed; use lowercase letters, digits and underscores."
    if kind not in (*SCALARS, "list") or terms.get("elements") not in (None, *SCALARS):
        return f"The input {option} takes {kind}, a free-form type; name each value it allows."
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
        return f"The input {option} cannot be read ({exc.errors(include_url=False)[0]['msg']})."
    inputs[option] = adopted
    text = adopted.elements if kind == "list" else kind
    if text in TEXT and not adopted.choices and bound is None:
        return f"The text input {option} needs its choices, or a pattern and length in its sidecar."
    return ""


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
    for scanned in proposal.procedures:
        name = scanned["sidecar"]
        declared = scanned["declaration"].get("id") if isinstance(scanned["declaration"], dict) else None
        problems = list(scanned["problems"])
        procedure = None if problems else _procedure(proposal, scanned, objects, used[name], shared, problems)
        if problems or procedure is None:
            unadoptable[name] = _Unadoptable(
                declared if isinstance(declared, str) else name,
                scanned["title"] or name,
                tuple(problems),
                frozenset(used[name]),
            )
            continue
        found[procedure.id] = procedure
    return found, unadoptable


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
    problems: list[str],
) -> AdoptedProcedure | None:
    """The playbook ``scanned`` as HQ would adopt it, with each problem that
    keeps it from being adopted added to ``problems``."""

    try:
        declaration = ProcedureDeclaration.model_validate_json(json.dumps(scanned["declaration"]))
        inputs = _inputs(scanned, declaration.inputs, problems)
        procedure = AdoptedProcedure(
            **declaration.model_dump(include=set(ProcedureTerms.model_fields)),
            playbook=scanned["playbook"],
            title=scanned["title"],
            preconditions=tuple(scanned["preconditions"]),
            verification=tuple(scanned["verification"]),
            inputs=inputs,
            revision=proposal.revision,
            files={path: objects[path] for path in sorted(used) if path in objects},
            shared=dict(shared),
        )
        manifest(procedure)
    except (ValidationError, ValueError) as exc:
        said = exc.errors(include_url=False)[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        problems.append(ended(said[:1].upper() + said[1:]))
        return None
    return procedure


def _does(effect: str) -> str:
    """What a procedure of ``effect`` does, in the words its command's page says it with."""

    return lower_first(effect_label(effect))


def titles(procedures: Mapping[str, AdoptedProcedure]) -> dict[str, str]:
    """Each playbook by the title you read it by, with its id beside a title
    two of them share, so the two stay apart."""

    return _apart({key: procedure.title for key, procedure in procedures.items()})


def _apart(titled: Mapping[str, str]) -> dict[str, str]:
    shared = Counter(titled.values())
    return {key: title if shared[title] == 1 else f"{title} ({key})" for key, title in titled.items()}


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
        gained.append(ended(f"{named} gains {access_words(after.privilege)}"))
    # A read's files too: a read held only by its declaration could be made to
    # read anything its login can by a change no person looked at.
    if after.files != before.files:
        gained.append(f"{named} runs different files.")
    moved = sorted(
        path for path in {*before.shared, *after.shared} if before.shared.get(path) != after.shared.get(path)
    )
    if any(PurePosixPath(path).name == REQUIREMENTS for path in moved):
        gained.append(f"{named} can use other Ansible collections.")
    if any(PurePosixPath(path).name != REQUIREMENTS for path in moved):
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
            if procedure.effect != "read" or procedure.privilege is not None:
                widens.append(ended(f"{called[key]} is new, and it {_does(procedure.effect)}"))
            continue
        widens.extend(_gains(was, procedure, called[key]))
    return widens


def _summary(procedure: AdoptedProcedure) -> str:
    """Its Effect, Runs on and Access, in the Playbooks table's words."""

    where = ", ".join(procedure.hosts) or anywhere(procedure.target_kinds)
    return dotted(effect_label(procedure.effect), where, access_words(procedure.privilege))


def _shown(procedure: AdoptedProcedure, named: str) -> dict[str, Any]:
    """A playbook as you compare it: ``named``, the name its group is headed
    by, and what it is at a glance, then each term in the words its command's
    page uses, each file by the start of its object id. Its playbook is among
    its files, so it is not said again."""

    return {
        SUMMARY: {"label": named, "value": _summary(procedure)},
        "effect": effect_label(procedure.effect),
        "runs_on": list(procedure.hosts) or anywhere(procedure.target_kinds),
        "access": access_words(procedure.privilege),
        "timeout": timeout_words(procedure.timeout_seconds),
        "retry": RETRY_WORDS[procedure.retry],
        "recovery": list(procedure.recovery),
        "preconditions": list(procedure.preconditions),
        "verification": list(procedure.verification),
        "fixes": [found.title if (found := rule_for(rule)) else rule for rule in procedure.fixes],
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
    material = json.dumps(
        {"procedures": [procedure.model_dump(mode="json") for procedure in procedures]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _commit_refusals(proposal: SourceProposal) -> list[str]:
    """Why HQ will not adopt the commit whatever its playbooks are."""

    refused = []
    if not proposal.verified or proposal.verification_reason != "valid":
        refused.append(f"GitHub did not verify the commit's signature ({proposal.verification_reason}).")
    refused.extend(
        ended(f"{path['path']} is {REFUSED_KINDS[path['kind']]}")
        for path in proposal.unpinned
        if path["kind"] in REFUSED_KINDS
    )
    return refused


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
            tuple(f"{sidecar}: {problem}" for problem in found.problems),
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
        raise RecordNotFound("No scan result has that ID. Refresh to scan again.")
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
                f"{sidecar}: {problem}"
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
    is named, else of every playbook whose change waits."""

    planned = judged(proposal_id)
    named = next((change for change in planned.changes if change.key == playbook), None)
    refused = (*planned.unplaced, *(named.problems if named else ())) if playbook else planned.refused
    if refused:
        raise PolicyError(
            "This can't be adopted; details.refused lists why. Fix it in the repository, then Refresh.",
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


# A playbook as a change of a scan names it: its id, or its sidecar where two declare one id.
PlaybookKey = Annotated[StrictStr, Field(min_length=1, max_length=200, title="Playbook")]


class AdoptProposal(BaseModel):
    """The scan result to adopt, and the one playbook to adopt from it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: Annotated[StrictInt, Field(ge=1, title="Scan result"), Describe("The scan result's ID.")]
    playbook: Annotated[
        PlaybookKey | None,
        Describe("The playbook whose change to adopt, by its id; left out, every change that waits is adopted."),
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
        raise PolicyError("That playbook's change was rejected. Refresh to scan again.")
    if playbook not in planned.procedures and playbook not in planned.before:
        raise RecordNotFound("No playbook of this scan result has that id.")
    return ()


@transaction.atomic
def adopt(command: AdoptProposal, *, principal: Principal) -> Adopted:
    """Adopt the change the proposal makes to one playbook, or every change
    that waits, so each runs from the proposal's commit; every other playbook
    stays as it was adopted."""

    with audit_operation(operation=ADOPT, principal=principal):
        planned = plan(command.proposal, command.playbook or "")
        chosen = _decided(planned, command.playbook)
        current = adopted()
        if not chosen:
            return {"ok": True, "adopted": False, "digest": current.digest if current else "", "widened": []}
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
    return {"ok": True, "adopted": True, "digest": row.digest, "widened": widened}


class Rejected(TypedDict):
    """What a rejection did: whether what it names waited to be decided until now."""

    ok: Literal[True]
    rejected: Annotated[bool, Describe("False when what it names was already rejected.")]


class RejectProposal(BaseModel):
    """The scan result to reject, and the one playbook to reject from it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: Annotated[StrictInt, Field(ge=1, title="Scan result"), Describe("The scan result's ID.")]
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
    adopted until the next Refresh reads another."""

    with audit_operation(operation=REJECT, principal=principal):
        proposal = SourceProposal.objects.select_for_update().filter(pk=command.proposal).first()
        if proposal is None:
            raise RecordNotFound("No scan result has that ID. Refresh to scan again.")
        if proposal.rejected_at is not None or command.playbook in proposal.rejected_playbooks:
            return {"ok": True, "rejected": False}
        chosen = _decided(judged(proposal.pk), command.playbook)
        if not chosen:
            raise PolicyError("Nothing in this scan result waits to be adopted, so there is nothing to reject.")
        if command.playbook is not None:
            proposal.rejected_playbooks = [*proposal.rejected_playbooks, command.playbook]
            proposal.save(update_fields=("rejected_playbooks", "updated_at"))
        else:
            proposal.rejected_at = timezone.now()
            proposal.rejected_by = principal.actor
            proposal.save(update_fields=("rejected_at", "rejected_by", "updated_at"))
    return {"ok": True, "rejected": True}


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
    """The automation source's commands: Refresh, which proposes, and Adopt and Reject, which decide."""

    return (
        *automation_sources.capability_specs(),
        CapabilitySpec(
            ADOPT,
            "Adopt what a scanned commit changes in the playbooks HQ runs: one playbook's change, or every "
            "change that waits. A commit GitHub did not verify, or one carrying an ansible.cfg or a plugins "
            "directory, is refused. Only a person adopts.",
            "infrastructure_change",
            # Held only at the operator's own interfaces (security.HELD_ONLY_AT).
            Capability.ADOPT_PLAYBOOKS,
            AdoptProposal,
            adopt,
            execution_notes=(
                "Refuse a commit GitHub did not verify or one carrying an ansible.cfg or a plugins directory.",
                "Refuse a playbook HQ can't run, and every change at once while one waits.",
                "Compare it, playbook by playbook, with the adopted playbooks.",
                "Each playbook adopted runs from this commit from now on; every other stays as adopted.",
                "A run of a changed playbook that waits for approval can no longer be approved.",
            ),
            label="Adopt the playbooks",
            verb="Adopt",
            home=automation_sources.PLAYBOOKS,
        ),
        CapabilitySpec(
            REJECT,
            "Reject what a scanned commit changes: one playbook's change, which stays as adopted, or the whole "
            "commit, so nothing waits to be adopted until a Refresh reads another. Only a person rejects.",
            "remote_write",
            Capability.ADOPT_PLAYBOOKS,
            RejectProposal,
            reject,
            execution_notes=(
                "Refuse a scan result with nothing waiting to be adopted.",
                "Mark the playbook's change or the scan result rejected, recorded under your name.",
            ),
            label="Reject a scanned commit",
            verb="Reject",
            home=automation_sources.PLAYBOOKS,
        ),
    )
