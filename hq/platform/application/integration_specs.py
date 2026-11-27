"""Leaf contracts shared by integration emitters and the graph compiler."""

import annotationlib
import inspect
from annotationlib import Format
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass, replace
from functools import cache
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    TypeAliasType,
    Union,
    cast,
    get_args,
    get_origin,
    is_typeddict,
)

from django.db.models import BigIntegerField, Q
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model

from .contracts import Effect
from .display_words import display
from .json_schemas import fields, sent_schema
from .reasons import declared_only
from .search_contracts import SearchDefinition
from .security import Capability, Principal, Requires
from .ui import ended
lazy from . import records, stored_columns
lazy from .labels import human_label, lower_first
lazy from .refusal_status import RefusalEnvelope
lazy from .resources import PageNumber, WholeLimit, several_of

if TYPE_CHECKING:
    from .entity_links import EntityLink
    from .service_context import ServiceSection


@dataclass(frozen=True, slots=True)
class TargetKind:
    """How a declared target binds to and is coerced for a capability, and
    the pattern a text target of the kind matches whole, which every door
    refuses the rest by and the API document states."""

    keyword: str
    coerce: Callable[[Any], Any]
    pattern: str = ""

    def binds[H: Callable[..., Any]](self, handler: H) -> H:
        """``handler``, written for any kind with its target as ``current``,
        taking it as this kind's keyword instead, so Python itself refuses any
        other keyword and its signature names the one it takes."""

        function = cast(FunctionType, handler)
        function.__code__ = function.__code__.replace(
            co_varnames=tuple(self._named(name) for name in function.__code__.co_varnames)
        )
        function.__kwdefaults__ = {self._named(name): value for name, value in (function.__kwdefaults__ or {}).items()}
        declared = function.__annotate__

        def annotate(wanted: Format) -> dict[str, Any]:
            found = annotationlib.call_annotate_function(declared, wanted) if declared else {}
            return {self._named(name): hint for name, hint in found.items()}

        function.__annotate__ = annotate
        return handler

    def _named(self, name: str) -> str:
        return self.keyword if name == "current" else name


# What an integer target may be sent as besides a number: the digits of an
# id, a digit fewer than the most a record's id may be, so every one is an id
# the number form takes.
INTEGER_TARGET = rf"^[1-9][0-9]{{0,{len(str(BigIntegerField.MAX_BIGINT)) - 2}}}$"

# A client the identity provider registered. A client known only by a Client
# ID Metadata Document is named by a URL, which this refuses, so one opt-in
# covering every such client can never reach a person's standing.
REGISTERED_CLIENT_ID = r"^[A-Za-z0-9._-]+$"

# One declaration drives spec validation, handler binding and request coercion.
TARGET_KINDS: dict[str, TargetKind] = {
    "slug": TargetKind("current_slug", str),
    "doc_id": TargetKind("current_doc_id", str),
    "integer": TargetKind("current_id", int, INTEGER_TARGET),
    "key": TargetKind("current_key", str),
    "client_id": TargetKind("current_client_id", str, REGISTERED_CLIENT_ID),
}


@cache
def command_adapter(command_type: type) -> TypeAdapter[Any]:
    """The one validator of a command and of the schema published for it, closed at every depth.

    A field the command does not have is refused, never dropped, so a
    misspelled or retired field cannot return success for work the caller did
    not ask for. A validator that says its own words is refused as it is
    built (``reasons.declared_only``), so every refusal of a command says a declared reason.
    """

    alias: Any = TypeAliasType(command_type.__name__, stored_columns.held(command_type))
    adapter = TypeAdapter(alias, config=ConfigDict(extra="forbid"))
    declared_only(adapter.core_schema, f"Command {command_type.__name__}")
    return adapter


@cache
def command_schema(command_type: type) -> dict[str, Any]:
    """Build an immutable command type's closed JSON Schema once; each field's
    ``command_fields`` markers write themselves in."""

    return sent_schema(command_adapter(command_type))


# The machine API's contract version (``/api/v2/``), which a retry key's
# request fingerprint names, so a key never replays across contracts.
CURRENT_API_VERSION = 2

# The one retry field every capability that changes or queues something takes
# over MCP, the header the machine API takes it in, and the one shape both hold to.
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
        "One key per logical change, kept when it is retried: a repeat returns the first result instead of "
        "acting again. A change sent without one is refused `idempotency_key_required`; only the host's shell "
        "is not asked."
    ),
}


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


# The runner's consent (``capabilities.Consented``), handed in place of the
# principal only to a handler that names it: one that writes a declaration.
CONSENT_KEYWORD = "consented"


def takes_consent(handler: Callable[..., Any]) -> bool:
    """Whether a capability handler declares ``consented`` by name: it writes a
    declaration, which only a consented act may."""

    return _declares(handler, CONSENT_KEYWORD)


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

    ``read`` answers the record's value for each field; the fields sent are
    laid over it and validated as one command. ``always_sent`` names a field
    the record cannot send back.
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
    and never requires it. A ``read`` takes none. One that keeps what it is
    not sent of the record it targets requires no field the record holds and
    gives none a default (``records.as_an_update_declares``).
    """

    schema = command_schema(spec.command_type)
    if kept := kept_by(spec):
        schema = records.as_an_update_declares(schema, kept.always_sent)
    if not spec.effect.changes:
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


def _declared(command: type) -> frozenset[str]:
    """The field names ``command`` declares, without evaluating its annotations."""

    if is_dataclass(command):
        return frozenset(each.name for each in dataclass_fields(command))
    return frozenset(getattr(command, "__required_keys__", ())) | frozenset(getattr(command, "__optional_keys__", ()))


@dataclass(frozen=True, slots=True)
class CapabilitySpec(Requires):
    name: str
    summary: str
    # Given as its value's string too, as an extension may write it.
    effect: Effect
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
    # The route of the page the command belongs to, for one whose records
    # are no resource of its own (a playbook's run belongs to Playbooks): its
    # page's trail and Cancel lead there instead of to Search.
    home: str = ""
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect", Effect(self.effect))
        # A type with a config of its own keeps it over the one
        # ``command_adapter`` closes a command with, so a command has none.
        command = self.command_type
        if issubclass(command, BaseModel) or "__pydantic_config__" in vars(command):
            raise TypeError(
                f"Capability {self.name!r} command {command.__name__} keeps a Pydantic config of its own; "
                "declare it a frozen dataclass or a TypedDict, which HQ closes."
            )
        # A retry is the runner's to answer once (``execute_once``), so no
        # handler is handed the key to answer it a second way.
        if IDEMPOTENCY_FIELD in _declared(command):
            raise TypeError(
                f"Capability {self.name!r} command {command.__name__} declares {IDEMPOTENCY_FIELD}; "
                "the capability runner keeps the retry key, so leave it out."
            )

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

    The annotation is the one declaration of a capability's result. An
    unresolvable one answers None, so an extension's result never fails the
    document that lists it.
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
    "the part named inside it, in every element of a list (`items.waiting`); `*` keeps every field. "
    "Absent, a list keeps the fields its description in `describe_resources` names as "
    "`operations.list.fields`, and a read of one record keeps the whole record."
)
# The query field a page past the first is asked by, read by the resource
# layer around the handler (application.resources) and never passed to it.
PAGE_FIELD = "page"


def typed_keys(kind: type) -> tuple[str, ...]:
    """A TypedDict's keys, its own and those it extends, in declared order."""

    # Read as forward references, so a type the annotations name need not be
    # importable yet when a spec is declared.
    return tuple(annotationlib.get_annotations(kind, format=Format.FORWARDREF))


# The query field a list's order is asked by, which a list that declares its
# sorts takes, and the one a searched kind's text is, each read by the resource
# layer around the handler and never passed to it.
SORT_FIELD = "sort"
SEARCH_FIELD = "query"


@dataclass(frozen=True, slots=True)
class ListSort:
    """One order a list is read in: its name in the query's ``sort``, which a
    column's heading links by (``date``, ``-date``), its label, and the
    ordering it stands for (a field or an annotation, ``-`` descending),
    where that is not the name, and an example where the items do not show
    the order: it makes records and answers their identifiers in it."""

    name: str
    label: str
    ordering: tuple[str, ...] = ()
    example: Callable[[], Iterable[object]] | None = None

    def __post_init__(self) -> None:
        if not self.ordering:
            object.__setattr__(self, "ordering", (self.name,))

    @classmethod
    def both(cls, name: str, label: str, reverse: str = "") -> tuple[ListSort, ListSort]:
        """The order ``name`` names and its reverse, said ``label`` and
        ``reverse``; a column of words names its own two from its heading
        alone, "Vendor A–Z" and "Vendor Z–A"."""

        if not reverse:
            label, reverse = f"{label} A–Z", f"{label} Z–A"
        return cls(name, label), cls(name[1:] if name.startswith("-") else f"-{name}", reverse)


# The values a list is narrowed by, as one choice: each value it takes and its words.
type Choices = Iterable[tuple[Any, str]]


@dataclass(frozen=True, slots=True)
class ListFilter:
    """One field a list is narrowed by to any of several values, sent
    comma-separated (``status=active,idea``): its name, its label, the values
    it takes with their words, or what reads them where they are the records'
    own (a year), and the lookup it narrows by where that is not its name, or
    the condition the values asked keep a row by where no one lookup says it.
    One whose values are read declares the ``pattern`` each is held to.
    ``unasked`` is what the list keeps while neither it nor a search is asked."""

    name: str
    label: str
    choices: Choices | Callable[[], Choices]
    lookup: str = ""
    pattern: str = ""
    unasked: Q | None = None
    narrows: Callable[[list[str]], Q] | None = None

    def __post_init__(self) -> None:
        if not self.lookup:
            object.__setattr__(self, "lookup", self.name)
        if callable(self.choices):
            if not self.pattern:
                raise ValueError(f"Filter {self.name!r} reads its values, so it declares the pattern they keep to.")
            return
        object.__setattr__(self, "choices", tuple(self.choices))
        if not self.pattern:
            object.__setattr__(self, "pattern", several_of(str(value) for value, _ in self.choices))

    def options(self) -> tuple[tuple[str, str], ...]:
        """Each value it takes as it is sent, and its words."""

        return tuple(
            (str(value), label) for value, label in (self.choices() if callable(self.choices) else self.choices)
        )


@dataclass(frozen=True, slots=True)
class ListToggle:
    """A flag a list is narrowed by (``no_receipts=true``): its name, its
    label, what it keeps in a sentence and as the condition that keeps it,
    and its example: it makes records and answers the identifiers of those
    the flag keeps and of those it leaves out. ``unasked`` is what the list
    keeps while the flag is off, for a flag that brings rows back."""

    name: str
    label: str
    description: str
    keeps: Q
    example: Callable[[], tuple[Iterable[object], Iterable[object]]] | None = None
    unasked: Q | None = None


@dataclass(frozen=True, slots=True)
class ListRead:
    """A resource's list read, declared whole: the reader that answers it,
    the query it takes, the TypedDict one item is whole (``fields=*``), and
    the keys each item keeps when the caller names none, so a page costs what
    it shows (every key of ``item`` where it names none). A list answers only
    its item's keys, and the API document types each item by it.

    A list that declares ``sorts`` takes ``sort``, one of their names, and is
    read in the first where it is asked none and is not searched. Each of its
    ``filters`` and ``toggles`` is a query field of its own, and ``totals``
    reads what the rows it matched add up to, answered beside its page."""

    # It may come from an extension, so its result is checked at the call
    # (application.resources) rather than trusted from the annotation.
    reader: Callable[..., object]
    query: type[BaseModel]
    item: type
    fields: tuple[str, ...] = ()
    sorts: tuple[ListSort, ...] = ()
    filters: tuple[ListFilter, ...] = ()
    toggles: tuple[ListToggle, ...] = ()
    totals: Callable[[Any], Mapping[str, Any]] | None = None
    # False for a list too long to count on every read: its pages say whether
    # more follow, and no total.
    counted: bool = True
    # Whether the resource layer cuts the page: a list derived whole, whose
    # query names no ``limit``, takes ``limit`` and ``page`` as every list does.
    cut: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if not self.fields and is_typeddict(self.item):
            object.__setattr__(self, "fields", typed_keys(self.item))
        object.__setattr__(self, "cut", "limit" not in self.query.model_fields)
        if self.sorts or self.filters or self.toggles or self.cut:
            object.__setattr__(self, "query", _asked_by(self))

    def ordering(self, name: str | None) -> tuple[str, ...]:
        """The ordering ``sort`` names; none for no name."""

        return next((sort.ordering for sort in self.sorts if sort.name == name), ())

    def keeps(self, asked: Mapping[str, Any], *, searched: bool) -> Q:
        """The condition the filters and toggles ``asked`` keep a row by."""

        kept = Q()
        for each in self.filters:
            if values := asked.get(each.name):
                chosen = values.split(",")
                kept &= each.narrows(chosen) if each.narrows else Q(**{f"{each.lookup}__in": chosen})
            elif each.unasked is not None and not searched:
                kept &= each.unasked
        for toggle in self.toggles:
            if asked.get(toggle.name):
                kept &= toggle.keeps
            elif toggle.unasked is not None:
                kept &= toggle.unasked
        return kept


def _asked_by(read: ListRead) -> type[BaseModel]:
    """``read``'s query taking a field for each filter and toggle it declares,
    and ``sort`` where it declares sorts, so the API document says each and any
    value they do not take is refused."""

    asked: dict[str, Any] = {
        each.name: (
            str | None,
            Field(
                default=None,
                title=each.label,
                max_length=2000,
                pattern=each.pattern,
                description=f"Only the records whose {lower_first(each.label)} is one of these, comma-separated; "
                "absent, every one.",
            ),
        )
        for each in read.filters
    }
    asked |= {
        toggle.name: (bool, Field(default=False, title=toggle.label, description=toggle.description))
        for toggle in read.toggles
    }
    if read.sorts:
        names = tuple(sort.name for sort in read.sorts)
        words = ended(
            "The order the records come in; absent, the best match first for a search, else "
            f"{lower_first(read.sorts[0].label)}"
        )
        choice: Any = Literal[names]
        asked[SORT_FIELD] = (Annotated[choice | None, Field(description=words)], None)
    if read.cut:
        # Absent, the page is the whole list, as the handler derives it.
        asked["limit"] = (WholeLimit, None)
        asked[PAGE_FIELD] = (PageNumber, 1)
    if declared := sorted(asked.keys() & read.query.model_fields.keys()):
        raise ValueError(f"{read.query.__name__} declares {declared}, which its list's declarations derive.")
    return create_model(read.query.__name__, __base__=read.query, __doc__=read.query.__doc__, **asked)


@dataclass(frozen=True, slots=True)
class DetailRead:
    """A resource's read of one record: the reader that answers it, and the
    TypedDict it answers, where it types one."""

    # Like a list's handler, checked at the call.
    reader: Callable[..., object]
    record: type | None = None


@dataclass(frozen=True, slots=True)
class ResourceSpec(Requires):
    """One declaration of a readable domain and every operation it supports."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    listing: ListRead | None = None
    detail: DetailRead | None = None
    identifier: str | None = None
    # A type, or one an ``Annotated`` bounds (``RecordId``).
    identifier_type: Any = str
    # RecordNotFound is answered as not found without being listed; these are
    # the other types a detail handler raises for a missing record, such as an
    # extension model's DoesNotExist.
    not_found_errors: tuple[type[Exception], ...] = ()
    search: SearchDefinition | None = None
    web_route: str = ""
    # The handlers also take ``principal=``: the answer depends on what the
    # caller may see, not only on whether it may read the resource.
    pass_principal: bool = False

    def __post_init__(self) -> None:
        # A search declared through ``SearchDefinition.of`` is this resource's
        # by name and label.
        if isinstance(self.search, SearchDefinition):
            found = replace(self.search, scope=self.search.scope or self.name, label=self.search.label or self.label)
            object.__setattr__(self, "search", found)

    @property
    def read_around(self) -> frozenset[str]:
        """The query fields the resource layer reads around the list's handler
        and never passes to it: the page, the sort, the filters and toggles,
        a searched kind's text, and a page's size where it cuts the page."""

        read = self.listing
        declared: list[str] = []
        if read:
            declared += [SORT_FIELD] if read.sorts else []
            declared += [each.name for each in read.filters] + [each.name for each in read.toggles]
            declared += ["limit"] if read.cut else []
        return frozenset({PAGE_FIELD, *declared, *((SEARCH_FIELD,) if self.search else ())})
