"""What a command's field says about itself beyond its type.

A field carries these markers in its ``Annotated`` type, and each writes
itself into the field's JSON Schema through pydantic's
``__get_pydantic_json_schema__``, the one shape every adapter reads: the
command page shows a description as the field's help and offers a
record-naming field's records, a reference's things, or a field's declared
choices, to choose from, and the machine contract publishes each. Inside a
named type alias (``type Notes = Annotated[str, Describe(...)]``) a marker is
written once, in the alias's definition, however many commands use it. They
are plain values rather than pydantic's ``Field``, so a module that declares
a command loads no validation library at startup.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, TypeAliasType, get_args, get_origin, get_type_hints

from .describe import Describe

# Lazy: it loads pydantic, which declaring a command does not.
lazy from .json_schemas import choices

if TYPE_CHECKING:
    from django.db.models import Model
    from pydantic import GetJsonSchemaHandler
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
    The form offers only these; the handler refuses any other on every path."""

    options: tuple[tuple[str, str], ...]

    def __init__(self, options: Iterable[tuple[Any, Any]]) -> None:
        object.__setattr__(self, "options", tuple((str(value), str(label)) for value, label in options))

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), **choices(self.options)}


@dataclass(frozen=True, slots=True, init=False)
class Refers:
    """A field whose value is a ``kind:identity`` reference of the kind
    ``model``'s ``ReferenceField`` called ``field`` holds:
    ``about: Annotated[str, Refers(Note, "about"), Describe("What it is about.")]``.

    The form offers what the record's own form offers, read from that one
    column's declaration, so the two cannot disagree. ``Names`` cannot say
    it: a reference spans several kinds, each offered only to a viewer who
    may name it.
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


@dataclass(frozen=True, slots=True)
class Upload:
    """A field that is a file, sent in JSON as its ``name``, ``content_type``
    and base64 ``content``: the form offers a file to pick and sends it so
    (``forms.file_as_sent``)."""

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), UPLOAD: True}


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


# Every marker is imported from here, Describe too, though it lives in a leaf of its own.
__all__ = [
    "AMONG",
    "NAMES_RECORDS",
    "REFERS",
    "UPLOAD",
    "Among",
    "AmongNow",
    "Choices",
    "Describe",
    "ForAgents",
    "Names",
    "Refers",
    "Upload",
    "among",
    "among_now",
    "for_agents",
    "is_upload",
    "named_records",
    "referred",
]
