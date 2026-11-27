"""What a command's field says about itself beyond its type.

Each marker writes itself into the field's JSON Schema, the one shape every
adapter reads. They are plain values rather than pydantic's ``Field``, so a
module that declares a command loads no validation library at startup.
"""

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, TypeAliasType, get_args, get_origin, get_type_hints

from django.core.exceptions import ValidationError
lazy from pydantic_core import core_schema

from .describe import Describe
from .non_empty import NonEmpty, RequiredText
from .reasons import Checked, Reason, checked

# Lazy: it loads pydantic, which declaring a command does not.
lazy from .json_schemas import choices

if TYPE_CHECKING:
    from django.db.models import Model
    from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
    from pydantic_core import CoreSchema

# The JSON Schema extension a record-naming field carries.
NAMES_RECORDS = "x-hq-records"
# The JSON Schema extension a record-naming field carries when only some of
# those records may be named.
AMONG = "x-hq-among"
# The JSON Schema extension a field holding a model's reference carries.
REFERS = "x-hq-reference"
# The JSON Schema extension a field holding a file carries.
UPLOAD = "x-hq-upload"
# The media types an upload takes, which a file picker offers.
ACCEPTS = "x-hq-accepts"
# The largest file an upload takes, in bytes.
MAX_BYTES = "x-hq-max-bytes"


@dataclass(frozen=True, slots=True)
class Names:
    """A field whose value is one of ``resource``'s records (several, as a list).

    ``key`` is the item field the value is, where that is not the resource's
    identifier:
    ``project: Annotated[str, Names("projects"), Describe("The project it is for.")]``.
    """

    resource: str
    key: str = ""

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {
            **handler(schema),
            NAMES_RECORDS: {"resource": self.resource, **({"key": self.key} if self.key else {})},
        }


@dataclass(frozen=True, slots=True, init=False)
class Among:
    """A record-naming field that may name only these of its records, beside
    its ``Names``: ``Annotated[tuple[str, ...], Names("machines"), Among(("app.example.com",))]``.

    The form offers those the caller may also read; the command's handler
    still refuses any other, since a caller may name a record another way.
    """

    values: tuple[str, ...]

    def __init__(self, values: Iterable[Any]) -> None:
        object.__setattr__(self, "values", tuple(str(value) for value in values))

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), AMONG: list(self.values)}


@dataclass(frozen=True, slots=True, init=False)
class Choices:
    """A field whose value is one of a fixed set declared elsewhere, each with
    the words a person reads for it: ``Choices(Appearance.Theme.choices)``.
    The form offers only these, and the command refuses any other as it is
    validated, before anything else is done with it."""

    options: tuple[tuple[Any, str], ...]

    def __init__(self, options: Iterable[tuple[Any, Any]]) -> None:
        object.__setattr__(self, "options", tuple((value, str(label)) for value, label in options))

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        # Pydantic's own Literal, once the value is of its field's type, so its refusal lists the choices.
        allowed = core_schema.literal_schema([value for value, _label in self.options])
        return core_schema.chain_schema([handler(source), allowed])

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), **choices(self.options)}


@dataclass(frozen=True, slots=True, init=False)
class Refers:
    """A field whose value is a ``kind:identity`` reference of the kind
    ``model``'s ``ReferenceField`` called ``field`` holds.

    The form offers what that column offers, so the two cannot disagree.
    Example: ``about: Annotated[str, Refers(Note, "about"), Describe("What it
    is about.")]``.
    """

    model: str
    field: str

    def __init__(self, model: type[Model], field: str) -> None:
        # Looked up now, so a misspelled column fails as the command is declared.
        model._meta.get_field(field)
        object.__setattr__(self, "model", model._meta.label_lower)
        object.__setattr__(self, "field", field)

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), REFERS: {"model": self.model, "field": self.field}}


@dataclass(frozen=True, slots=True)
class AmongNow:
    """A record-naming field whose form offers only the records ``values``
    answers as the form is drawn, beside its ``Names`` and any ``Among``: what
    the records' state allows at that moment (the machines a worker reaches)
    rather than what the command's type declares. Never in the schema, since
    the answer moves; the command's handler refuses any other on every path."""

    values: Callable[[], Iterable[str]]


def among_now(kind: type) -> dict[str, Callable[[], Iterable[str]]]:
    """Each field of ``kind`` marked ``AmongNow``, to what answers its records."""

    found = {name: _marker(hint, AmongNow) for name, hint in get_type_hints(kind, include_extras=True).items()}
    return {name: marker.values for name, marker in found.items() if isinstance(marker, AmongNow)}


# Where ``stored_in`` puts the model on the command it declares.
STORED_IN = "__stored_in__"


def stored_in[Command: type](model: type[Model]) -> Callable[[Command], Command]:
    """Declare the model a command that is no record domain's writes, so each
    of its fields named for one of the model's columns is held to that
    column's rules as the command is validated (``records.stored_rules``)."""

    def declare(command: Command) -> Command:
        setattr(command, STORED_IN, model)
        return command

    return declare


@dataclass(frozen=True, slots=True)
class ForAgents:
    """A field a client sends and a person is never asked, such as an agent's
    own id for an investigation a run belongs to: the web form leaves it out,
    and every other door takes it. Only a field a command may be sent without
    can be one."""


def for_agents(kind: type) -> frozenset[str]:
    """The fields of ``kind`` marked ``ForAgents``."""

    hints = get_type_hints(kind, include_extras=True)
    return frozenset(name for name, hint in hints.items() if _marker(hint, ForAgents) is not None)


def names_of(hint: Any) -> Names | None:
    """The ``Names`` a field's type carries, when it names other records."""

    return _marker(hint, Names)


def _marker[T](hint: Any, marker: type[T]) -> T | None:
    """The ``marker`` a field's type carries, in its own ``Annotated`` or an
    alias it names, the nearest the field first."""

    if isinstance(hint, TypeAliasType):
        return _marker(hint.__value__, marker)
    if get_origin(hint) is Annotated:
        inner, *markers = get_args(hint)
        held = [found for found in markers if isinstance(found, marker)]
        return held[-1] if held else _marker(inner, marker)
    return None


# Why a file past its ``Upload``'s limit is refused; its schema's ``x-hq-max-bytes`` says the limit.
_TOO_LARGE = Reason("too_large", "is larger than the largest file it takes")


def _content(file: Any) -> object:
    return file.get("content") if isinstance(file, dict) else getattr(file, "content", None)


@dataclass(frozen=True, slots=True, kw_only=True)
class Upload:
    """A field that is a file, sent in JSON as its ``name``, ``content_type``
    and base64 ``content``: the form offers a file to pick and sends it so
    (``forms.file_as_sent``). ``accepts`` names the media types it takes,
    which the form's picker offers, and ``max_bytes`` the largest file, which
    the picker refuses before sending and ``uploads`` sums with every form's.
    It may mark a list's items, whose ``max_length`` says how many one call sends.

    It holds the file to ``max_bytes`` on every door: its ``content`` as sent,
    before anything decodes it, to the longest text such a file encodes to,
    and its bytes, where its type decodes them, to ``max_bytes`` itself."""

    max_bytes: int
    accepts: tuple[str, ...] = ()

    @property
    def max_encoded(self) -> int:
        """The longest base64 text a file within ``max_bytes`` encodes to, as a call sends it."""

        return 4 * math.ceil(self.max_bytes / 3)

    def _within(self, sent: Any, inner: Callable[[Any], Any]) -> Any:
        content = _content(sent)
        if isinstance(content, str | bytes) and len(content) > self.max_encoded:
            raise _TOO_LARGE.refused()
        file = inner(sent)
        content = _content(file)
        if isinstance(content, bytes) and len(content) > self.max_bytes:
            raise _TOO_LARGE.refused()
        return file

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return Checked(self._within, mode="wrap").__get_pydantic_core_schema__(source, handler)

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        accepts = {ACCEPTS: sorted(self.accepts)} if self.accepts else {}
        return {**handler(schema), UPLOAD: True, MAX_BYTES: self.max_bytes, **accepts}


def upload_of(hint: Any) -> Upload | None:
    """The ``Upload`` a field's type carries, when it is a file."""

    return _marker(hint, Upload)


_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_day(value: object) -> object:
    # The day itself, since what follows a validator reads a strict date only
    # as one, never as its text.
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DAY.fullmatch(value):
        return date.fromisoformat(value)
    raise ValueError


# The type of every day a caller sends: the ISO 8601 date its schema's
# ``format: date`` says, since Pydantic would read a number, or a text of
# digits, as a Unix time.
Day = Annotated[date, Checked(_iso_day, Reason("iso_day", "must be an ISO 8601 date, YYYY-MM-DD"), mode="before")]


# A delete's confirmation: the identifier of what it deletes, sent again, so
# no caller deletes a record it named only once. ``confirm`` checks it.
type Confirm = Annotated[str, Describe("The identifier of what it deletes, again, to confirm the delete.")]

CONFIRM_MISMATCH = "Must match the identifier of what it deletes."


def confirm(given: str, target: str | int) -> None:
    """Refuse a delete whose ``Confirm`` field, named ``confirm``, is not ``target``'s identifier."""

    if given != str(target):
        raise ValidationError({"confirm": ValidationError(CONFIRM_MISMATCH, code="invalid")})


def is_upload(schema: dict[str, Any]) -> bool:
    """Whether one field's schema is a file a person picks."""

    return schema.get(UPLOAD) is True


def named_records(schema: dict[str, Any]) -> dict[str, Any] | None:
    """The records one field's schema names, or ``None``."""

    declared = schema.get(NAMES_RECORDS)
    return declared if isinstance(declared, dict) else None


def among(schema: dict[str, Any]) -> frozenset[str] | None:
    """The only records one record-naming field may name, or ``None`` for any."""

    declared = schema.get(AMONG)
    return frozenset(declared) if isinstance(declared, list) else None


def referred(schema: dict[str, Any]) -> tuple[str, str] | None:
    """``(model label, field)`` of the reference one field's schema holds, or ``None``."""

    declared = schema.get(REFERS)
    return (declared["model"], declared["field"]) if isinstance(declared, dict) else None


# Every marker is imported from here, Describe, NonEmpty and Checked too, though each lives in a leaf of its own.
__all__ = [
    "AMONG",
    "CONFIRM_MISMATCH",
    "MAX_BYTES",
    "NAMES_RECORDS",
    "REFERS",
    "UPLOAD",
    "Among",
    "AmongNow",
    "Checked",
    "Choices",
    "Confirm",
    "Describe",
    "ForAgents",
    "Names",
    "NonEmpty",
    "Reason",
    "Refers",
    "RequiredText",
    "Upload",
    "among",
    "among_now",
    "checked",
    "confirm",
    "for_agents",
    "is_upload",
    "named_records",
    "referred",
    "stored_in",
    "upload_of",
]
