"""What an automation procedure is, as one operation manifest, and the registry that answers one by id.

What each field holds and why: docs/Architecture.md, "Operation manifests".
"""

import re
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from hq.domains.control_plane.names import is_hostname, normalized_hostname
from hq.platform.application.contracts import DOTTED_NAME, Closed, Effect
from hq.platform.application.non_empty import NonEmpty, RequiredText
from hq.platform.application.quoted import Quoted
from hq.platform.application.reasons import Checked, Reason, checked

# An hour: a procedure that needs longer is more than one procedure, and a
# stalled run holds what it acts on no longer than this.
LONGEST_TIMEOUT_SECONDS = 3600

# Names that choose what runs, where, as whom or with what, in Ansible Runner's
# own spelling (``RunnerConfig`` and ``BaseConfig`` in ansible-runner 2.4.3)
# and Ansible's, compared without case or separators. A run's playbook,
# inventory and connection come from its manifest and HQ's facts, so a
# parameter by one of these names is a free execution field.
EXECUTION_FIELDS = frozenset(
    {
        "become",
        "becomemethod",
        "becomeuser",
        "binary",
        "callback",
        "callbackplugin",
        "callbacks",
        "cmdline",
        "collection",
        "collections",
        "collectionspath",
        "commandline",
        "connection",
        "containerimage",
        "containeroptions",
        "containervolumemounts",
        "delegateto",
        "envvars",
        "extravars",
        "host",
        "hostpattern",
        "hosts",
        "inventory",
        "inventorypath",
        "limit",
        "module",
        "moduleargs",
        "passwords",
        "playbook",
        "playbookpath",
        "privatedatadir",
        "processisolation",
        "processisolationexecutable",
        "projectdir",
        "rolespath",
        "skiptags",
        "sshargs",
        "sshcommonargs",
        "sshextraargs",
        "sshkey",
        "sshoptions",
        "tags",
        "vaultpassword",
        "vaultpasswordfile",
    }
)

# A full commit: an abbreviation can come to name a second commit.
REVISION = r"^[0-9a-f]{40}$"

# A playbook's path inside the reviewed commit, in one spelling: relative,
# each part starting with a letter, digit or underscore (so no `.` or `..`
# part, no empty one, and nothing ansible-playbook reads as an option), and a
# YAML file. Which repository holds the project is the host's own
# configuration, never a manifest's.
PLAYBOOK = r"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.yml$"
PLAYBOOK_LONGEST = 200


_HOST_NAMES = Reason("host_names", "must each be a host name in HQ's spelling, lowercase with no trailing dot")
_CLOSED = Reason("input_closed", "must take no field beyond its own: base it and each model it holds on Closed")
_NO_EXECUTION_FIELD = Reason(
    "input_execution_field", "must name no execution field, which the manifest and HQ's facts choose"
)
_NAMED_FIELDS = Reason("input_free_form", "must name each field it allows, never take free-form values")
_READ_PRIVILEGE = Reason("read_privilege", "is a read, which takes no privilege")
_INVESTIGATION_READ = Reason("investigation_read", "must be read-only to investigate a finding")
_SETTLED_CHANGE = Reason("settled_change", "names a read that settles it, which only a change hands off to")
_CHANGE_CHECKED = Reason(
    "change_checked",
    "is a change, which needs preconditions and verification: its play's pre_tasks, and its post_tasks or those "
    "of the read its settled_by names",
)


def host_names(names: tuple[str, ...]) -> tuple[str, ...]:
    """``names`` as host names in HQ's spelling, sorted once each; refuses any other."""

    for name in names:
        if name != normalized_hostname(name) or not is_hostname(name):
            raise _HOST_NAMES.refused()
    return sorted_unique(names)


def sorted_unique(names: tuple[str, ...]) -> tuple[str, ...]:
    """``names`` sorted, each once, so two spellings of one set compare equal."""

    return tuple(sorted(set(names)))


DottedName = Annotated[str, StringConstraints(pattern=DOTTED_NAME.pattern)]
# A finding rule's name, in the spelling every rule has (``filesystem-nearly-full``).
RuleName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]
# What a playbook's author wrote (a title, a statement, an input's words),
# held as ``Quoted`` so every page shows it as written: last of a field's terms.
AUTHORED = Checked(Quoted)
Statement = Annotated[RequiredText, StringConstraints(strip_whitespace=True, max_length=500), AUTHORED]
Statements = Annotated[tuple[Statement, ...], NonEmpty()]


def _annotations(annotation: Any) -> Iterator[Any]:
    yield annotation
    for argument in get_args(annotation):
        yield from _annotations(argument)


def _closed(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    """Refuse an input model a caller could hand anything beyond its declared fields."""

    if model in seen:
        return
    seen.add(model)
    if model.model_config.get("extra") != "forbid":
        raise _CLOSED.refused()
    for name, field in model.model_fields.items():
        spellings = {name, field.alias, field.validation_alias if isinstance(field.validation_alias, str) else None}
        if {re.sub(r"[^a-z0-9]", "", spelling.lower()) for spelling in spellings if spelling} & EXECUTION_FIELDS:
            raise _NO_EXECUTION_FIELD.refused()
        for annotation in _annotations(field.annotation):
            kind = get_origin(annotation) or annotation
            if annotation is Any or annotation is object or (isinstance(kind, type) and issubclass(kind, Mapping)):
                raise _NAMED_FIELDS.refused()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _closed(annotation, seen)


# The input a procedure and the read that settles it (``settled_by``) both
# take: the commit a run applies, which the read is asked at.
REVISION_INPUT = "revision"

# What a playbook may do: it acts on machines, so it never only changes a record.
PlaybookEffect = Literal[Effect.READ, Effect.INFRASTRUCTURE_CHANGE, Effect.DESTRUCTIVE]


class ProcedureTerms(Closed):
    """What a playbook's manifest and its sidecar both say of it: where it
    may run, what it does, and how to recover from a failed run."""

    model_config = ConfigDict(frozen=True, strict=True)

    id: DottedName
    target_kinds: Annotated[tuple[DottedName, ...], NonEmpty(), Checked(sorted_unique)]
    # Empty: any host of those kinds.
    hosts: Annotated[tuple[str, ...], Checked(host_names)] = ()
    # None, or the reviewed profile a run escalates through.
    privilege: DottedName | None
    effect: PlaybookEffect
    timeout_seconds: Annotated[int, Field(ge=1, le=LONGEST_TIMEOUT_SECONDS)]
    recovery: Statements
    retry: Literal["never", "observe_first", "safe"]
    # The findings a run of it fixes, by rule: a finding of one about a
    # machine it may run on offers its Run page as a remedy. It grants nothing:
    # the run is asked for, approved and held as any other.
    fixes: Annotated[tuple[RuleName, ...], Checked(sorted_unique)] = ()
    # Read-only playbooks can offer evidence for a finding without claiming to clear it.
    investigates: Annotated[tuple[RuleName, ...], Checked(sorted_unique)] = ()
    # The kinds of reading a run of it states (``set_stats`` ``readings``), each
    # one HQ declares the words of, so whoever asks for a run knows what to read
    # back. Empty: it states its result alone.
    readings: Annotated[tuple[DottedName, ...], Checked(sorted_unique)] = ()
    # The read that settles a run of it once its playbook hands off to a unit
    # it leaves running (``RunStated.handoff``): a procedure of effect read, of
    # the same target kinds, taking every revision its own revision input
    # takes (``automation_adoption._unsettled``).
    # None: a run of it that hands off is refused.
    settled_by: DottedName | None = None

    @model_validator(mode="after")
    @checked
    def _read_needs_no_privilege(self) -> Self:
        if not self.effect.changes and self.privilege is not None:
            raise _READ_PRIVILEGE.refused()
        if self.investigates and self.effect != Effect.READ:
            raise _INVESTIGATION_READ.refused()
        if self.settled_by is not None and (not self.effect.changes or self.settled_by == self.id):
            raise _SETTLED_CHANGE.refused()
        return self


class OperationManifest(ProcedureTerms):
    """One reviewed playbook, as HQ may run it."""

    revision: Annotated[str, StringConstraints(pattern=REVISION)]
    playbook: Annotated[str, StringConstraints(pattern=PLAYBOOK, max_length=PLAYBOOK_LONGEST)]
    input_type: type[BaseModel]
    # The names of the play's own assertions before and after its work, and
    # after, those of the read that settles it (``settled_by``), whose report
    # is its run's. A read may state none; a change states each.
    preconditions: tuple[Statement, ...]
    verification: tuple[Statement, ...]

    @field_validator("input_type")
    @classmethod
    @checked
    def _input_is_closed(cls, model: type[BaseModel]) -> type[BaseModel]:
        _closed(model, set())
        return model

    @model_validator(mode="after")
    @checked
    def _a_change_says_how_it_is_checked(self) -> Self:
        if self.effect.changes and not (self.preconditions and self.verification):
            raise _CHANGE_CHECKED.refused()
        return self

    def admits(self, kind: str, host: str) -> bool:
        """Whether a run may target this host of this kind."""

        return kind in self.target_kinds and (not self.hosts or host in self.hosts)


class UnknownOperation(KeyError):
    """No manifest in the registry has this id."""


class DuplicateOperation(ValueError):
    """Two manifests have the same id."""


class OperationRegistry(Mapping[str, OperationManifest]):
    """The manifests HQ was supplied, by id."""

    __slots__ = ("_by_id",)

    def __init__(self, manifests: tuple[OperationManifest, ...]) -> None:
        by_id: dict[str, OperationManifest] = {}
        for manifest in manifests:
            if manifest.id in by_id:
                raise DuplicateOperation(f"Two operation manifests have the id {manifest.id!r}.")
            by_id[manifest.id] = manifest
        self._by_id = MappingProxyType(by_id)

    def __getitem__(self, operation_id: str) -> OperationManifest:
        try:
            return self._by_id[operation_id]
        except KeyError:
            raise UnknownOperation(operation_id) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)
