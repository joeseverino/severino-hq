"""Leaf contracts shared by integration emitters and the graph compiler."""

import annotationlib
import inspect
from annotationlib import Format
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, TypeAliasType, Union, get_args, get_origin, is_typeddict

from pydantic import BaseModel, ConfigDict, TypeAdapter

from .display_words import display
from .json_schemas import fields, json_schema
from .search_contracts import SearchDefinition
from .security import Capability, Principal
from .ui import ended
lazy from . import records
lazy from .labels import human_label, lower_first
lazy from .refusal_status import RefusalEnvelope

if TYPE_CHECKING:
    from .entity_links import EntityLink
    from .service_context import ServiceSection


@dataclass(frozen=True, slots=True)
class TargetKind:
    """How a declared target binds to and is coerced for a capability."""

    keyword: str
    coerce: Callable[[Any], Any]


# One declaration drives spec validation, handler binding and request coercion.
TARGET_KINDS: dict[str, TargetKind] = {
    "slug": TargetKind("current_slug", str),
    "doc_id": TargetKind("current_doc_id", str),
    "integer": TargetKind("current_id", int),
    "key": TargetKind("current_key", str),
}


@cache
def command_adapter(command_type: type) -> TypeAdapter[Any]:
    """The one validator of a command and of the schema published for it,
    closed at every depth: a field the command does not have is refused, never
    dropped, so a misspelled or retired field cannot return success for work
    the caller did not ask for.

    A stdlib dataclass or TypedDict with no config of its own takes the config
    of whatever holds it, so the command and every such type inside it take
    this one. Pydantic accepts a config only for a type that has none of its
    own to hold, which an alias of the command is. A Pydantic model keeps its
    own config: an extension's is a ``StrictCommand``.
    """

    alias: Any = TypeAliasType(command_type.__name__, command_type)
    return TypeAdapter(alias, config=ConfigDict(extra="forbid"))


@cache
def command_schema(command_type: type) -> dict[str, Any]:
    """Build an immutable command type's closed JSON Schema once; each field's
    ``command_fields`` markers write themselves in."""

    return json_schema(command_adapter(command_type))


# The machine API's contract version (``/api/v2/``), which a retry key's
# request fingerprint names, so a key never replays across contracts.
CURRENT_API_VERSION = 2

# The one retry field every capability that changes or queues something takes,
# the header that is its other spelling, and the one shape both hold to.
IDEMPOTENCY_FIELD = "idempotency_key"
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_LENGTH = 128
IDEMPOTENCY_KEY_PATTERN = rf"^[A-Za-z0-9][A-Za-z0-9._:-]{{0,{IDEMPOTENCY_KEY_LENGTH - 1}}}$"
IDEMPOTENCY_KEY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": IDEMPOTENCY_KEY_LENGTH,
    "pattern": IDEMPOTENCY_KEY_PATTERN,
    "examples": ["7d9f3c2e-4b1a-4e8f-9a6d-0c5b2e1f8a73"],
}
IDEMPOTENCY_FIELD_SCHEMA: dict[str, Any] = {
    **IDEMPOTENCY_KEY_SCHEMA,
    "description": (
        f"One key per logical change, kept when it is retried: a repeat returns the first result instead of "
        f"acting again. It is sent as the payload's `{IDEMPOTENCY_FIELD}` or the `{IDEMPOTENCY_HEADER}` header; "
        "a change needs one of the two, and is refused `idempotency_key_required` with neither. Only the host's "
        "shell is not asked."
    ),
}


def declares_idempotency_key(command_type: type) -> bool:
    """Whether the command carries the key into its own domain operation."""

    return IDEMPOTENCY_FIELD in command_schema(command_type).get("properties", {})


# Where the record a command acts on goes in what asking for it wants done.
TARGET = "{target}"


# A command's ``reached_from`` when the page of whatever it acts on sends
# it, so search does not offer it.
ON_ITS_PAGE = "its own page"


# The caller's stale-write token, handed only to a handler that names it.
STALE_WRITE_KEYWORD = "expected_updated_at"


# The names of the fields the caller sent, handed only to a handler that names
# it, so a write to a record it holds sets those and keeps the rest.
SENT_KEYWORD = "sent"


def takes_stale_write_token(handler: Callable[..., Any]) -> bool:
    """Whether a capability handler declares ``expected_updated_at`` by name.

    Only a handler that checks for a stale write declares it; every other
    handler is called without it.
    """

    return _declares(handler, STALE_WRITE_KEYWORD)


def takes_sent_fields(handler: Callable[..., Any]) -> bool:
    """Whether a capability handler declares ``sent`` by name: it writes only
    the fields its caller sent to a record that exists."""

    return _declares(handler, SENT_KEYWORD)


@dataclass(frozen=True, slots=True)
class Kept:
    """What a write to the record it targets keeps, so it needs only its target.

    ``read`` takes the target, as its kind coerces it, and answers the record's
    value for each field of the command, in the shape a caller sends it. The
    fields sent are laid over that and the whole is validated as one command
    (``capabilities.validate_command``). ``always_sent`` names a field the
    record holds nothing to send back for (a replaced file), which the write
    still requires.
    """

    read: Callable[[Any], Mapping[str, Any]]
    always_sent: frozenset[str] = frozenset()


# Where ``keeps`` puts its declaration on a handler.
_KEPT = "kept"


def keeps[Handler: Callable[..., Any]](kept: Kept) -> Callable[[Handler], Handler]:
    """Declare what a handler that writes only the fields it is ``sent`` keeps
    of the record it targets."""

    def declare(handler: Handler) -> Handler:
        setattr(handler, _KEPT, kept)
        return handler

    return declare


def kept_by(spec: CapabilitySpec) -> Kept | None:
    """What a capability keeps of the record it targets, or None for one that
    targets none or keeps nothing."""

    kept = getattr(spec.handler, _KEPT, None) if spec.target_kind else None
    return kept if isinstance(kept, Kept) else None


@cache
def _declares(handler: Callable[..., Any], keyword: str) -> bool:
    parameter = inspect.signature(handler, annotation_format=Format.STRING).parameters.get(keyword)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def capability_schema(spec: CapabilitySpec) -> dict[str, Any]:
    """The input a capability accepts: its command's schema and the retry key.

    A capability whose effect is not ``read`` takes an optional
    ``idempotency_key``, whether or not its command type declares the field,
    and never requires it. A ``read`` takes none. A record command's fields
    say what its model allows (``records.as_its_model_declares``), and one
    that keeps what it is not sent of the record it targets requires no field
    the record holds and gives none a default (``records.as_an_update_declares``).
    """

    schema = records.as_its_model_declares(spec.command_type, command_schema(spec.command_type))
    if kept := kept_by(spec):
        schema = records.as_an_update_declares(schema, kept.always_sent)
    if spec.effect == "read":
        return schema
    properties = dict(schema.get("properties", {}))
    properties[IDEMPOTENCY_FIELD] = dict(IDEMPOTENCY_FIELD_SCHEMA)
    derived = {**schema, "properties": properties}
    required = [name for name in schema.get("required", ()) if name != IDEMPOTENCY_FIELD]
    if required:
        derived["required"] = required
    else:
        derived.pop("required", None)
    return derived


def capability_fields(spec: CapabilitySpec) -> dict[str, dict[str, Any]]:
    """Each field a capability accepts, its schema read whole."""

    return fields(capability_schema(spec))


@display
class CommandFact:
    """One thing a command's page states about what the command is."""

    label: str
    value: str = ""
    # Lines under the value, one each.
    detail: tuple[str, ...] = ()
    # The things it names, each drawn as its link in place of ``value``.
    links: tuple[EntityLink, ...] = ()


@display
class CommandAbout:
    """What a command's page shows beside its form: the facts of what it is,
    under ``label``, and the sections of how it has gone (its recent runs)."""

    label: str = ""
    facts: tuple[CommandFact, ...] = ()
    sections: tuple[ServiceSection, ...] = ()

    def __post_init__(self) -> None:
        if self.facts and not self.label.strip():
            raise ValueError("A command's facts are drawn under a heading: give CommandAbout a label.")


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    summary: str
    effect: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    command_type: type
    handler: Callable[..., Mapping[str, Any]]
    target_kind: str | None = None
    subject_resource: str | None = None
    target_label: str = ""
    target_help: str = ""
    target_query: tuple[tuple[str, str | int | float | bool], ...] = ()
    execution_notes: tuple[str, ...] = ()
    target_initial_fields: tuple[str, ...] = ()
    # What a person calls the command. Optional so an extension that has not
    # named its commands still composes; those read as their dotted name.
    label: str = ""
    # What its button says, where that is shorter than its name ("Remove"
    # beside "Remove a record"). Every button, link and toast for the command
    # reads its words from here, never from text of its own.
    verb: str = ""
    # The words for a mode or state of the command, by name, where one
    # button cannot say them all: ``(("all", "Refresh all"),)``.
    variants: tuple[tuple[str, str], ...] = ()
    # The route of the page a person starts the command from, for one that
    # needs a value only that page hands out (a held change's id, a token HQ
    # gave a live record), or ``ON_ITS_PAGE`` for one the page of whatever it
    # acts on sends. Search links there instead of to the command's form, and
    # the form says where to start.
    reached_from: str = ""
    # What asking for the command wants done, read after "example-agent
    # wants to", with ``{target}`` where the record it acts on goes: "keep
    # the live version of {target}". Only for a name that does not read
    # that way with its target after it (``wanted``).
    wants: str = ""
    # Where a switch stands now, for one whose state no record's detail holds:
    # called with its target when it takes one, else with nothing. The command
    # page says it and offers only the button that changes it.
    state: Callable[..., bool] | None = None
    # What the command's page says of it beside its form, read when the page
    # is drawn, so what it lists (a run that just ended) is current, and
    # given its reader, so it lists only what they may read.
    about: Callable[[Principal], CommandAbout] | None = None

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)

    @property
    def title(self) -> str:
        """The command's name wherever a person reads it."""

        return self.label or human_label(self.name)

    def wanted(self, variant: str = "") -> str:
        """What asking for the command wants done, ``{target}`` where its
        record goes: its declared ``wants``, else its words mid-sentence with
        the target after them ("delete the message {target}" from "Delete a
        message"), or a variant's words for it ("turn off updates for
        {target}")."""

        if variant:
            phrase = lower_first(self.words(variant))
            return f"{phrase} for {TARGET}" if self.target_kind else phrase
        if self.wants:
            return self.wants
        phrase = lower_first(self.title)
        if not self.target_kind:
            return phrase
        verb, _, rest = phrase.partition(" ")
        article, _, noun = rest.partition(" ")
        if article in ("a", "an") and noun:
            phrase = f"{verb} the {noun}"
        return f"{phrase} {TARGET}"

    def words(self, variant: str = "") -> str:
        """What a button for the command says: its verb, else its name, or
        one of its declared variants. An undeclared variant is a KeyError."""

        if variant:
            return dict(self.variants)[variant]
        return self.verb or self.title

    def requested(self, subject: str = "", *, again: bool = False) -> str:
        """What a press says once the command is asked for, in its own words."""

        return requested(self.words(), subject, again=again)


def answer_type(spec: CapabilitySpec) -> Any | None:
    """What ``spec`` answers when it runs, the TypedDict its handler's return
    annotation names, or None where that names no precise shape.

    The annotation is the one declaration of a capability's result, so every
    door that publishes it reads it from there. A refusal the handler returns
    rather than raises (``RefusalEnvelope``) is the runner's to answer, so it
    is no part of the result. A plain dict, or an annotation that cannot be
    resolved (an extension's name for a type it cannot import here), answers
    None: such a capability is published as answering an object, and never
    fails the document that lists it.
    """

    try:
        declared = inspect.signature(spec.handler, eval_str=True).return_annotation
        while isinstance(declared, TypeAliasType):
            declared = declared.__value__
    except Exception:  # noqa: BLE001 - whatever an extension's annotation raises, its answer is an object
        return None
    members = tuple(
        member
        for member in (get_args(declared) if get_origin(declared) is Union else (declared,))
        if member is not RefusalEnvelope
    )
    if not members or not all(is_typeddict(member) for member in members):
        return None
    return members[0] if len(members) == 1 else Union[members]  # noqa: UP007 - built from a tuple of types


def requested(words: str, subject: str = "", *, again: bool = False) -> str:
    """What a press of the button saying ``words`` says once its work is asked
    for: the one sentence every action's toast is."""

    about = f" for {subject}" if subject else ""
    return ended(f"{words} {'was already requested' if again else 'requested'}{about}")


# What every adapter reads beside a resource query as the keys to return, so
# no query model may declare it (integration_validation), and what it keeps,
# as every door that takes it says so.
RESERVED_QUERY_FIELD = "fields"
FIELD_MASK = (
    "The fields of each record to keep: a name keeps that whole value (`observed_at`), and a dot keeps only "
    "the part named inside it, in every element of a list (`items.waiting`); `*` alone keeps every field. "
    "Absent, a list keeps the fields its description in `describe_resources` names as "
    "`operations.list.fields`, and a read of one record keeps the whole record."
)
# The query field a page past the first is asked by, read by the resource
# layer around the handler (application.resources) and never passed to it.
PAGE_FIELD = "cursor"


def typed_keys(kind: type) -> tuple[str, ...]:
    """A TypedDict's keys, its own and those it extends, in declared order."""

    # Read as forward references, so a type the annotations name need not be
    # importable yet when a spec is declared.
    return tuple(annotationlib.get_annotations(kind, format=Format.FORWARDREF))


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One declaration of a readable domain and every operation it supports."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    # Handlers may come from an extension, so their result is checked at the
    # call (application.resources) rather than trusted from the annotation.
    list_handler: Callable[..., object] | None = None
    list_query_type: type[BaseModel] | None = None
    detail_handler: Callable[..., object] | None = None
    identifier: str | None = None
    identifier_type: type = str
    # RecordNotFound is answered as not found without being listed; these are
    # the other types a detail handler raises for a missing record, such as an
    # extension model's DoesNotExist.
    not_found_errors: tuple[type[Exception], ...] = ()
    search: SearchDefinition | None = None
    web_route: str = ""
    # The handlers also take ``principal=``: the answer depends on what the
    # caller may see, not only on whether it may read the resource.
    pass_principal: bool = False
    # The fields each item of a list read keeps when the caller names none, so
    # a page costs what it shows; empty keeps the whole item. The whole record
    # is the detail read, or a list read's ``fields``. Derived from
    # ``row_type`` where the spec declares one.
    list_fields: tuple[str, ...] = ()
    # The TypedDict one item of a list read is, as it keeps ``list_fields``,
    # and the one a read of one record answers, which extends it. The API
    # document types each read's answer by them.
    row_type: type | None = None
    record_type: type | None = None

    def __post_init__(self) -> None:
        if self.row_type is not None and not self.list_fields and is_typeddict(self.row_type):
            object.__setattr__(self, "list_fields", typed_keys(self.row_type))

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)
