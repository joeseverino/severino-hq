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

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
    "Choices",
    "Describe",
    "Names",
    "Refers",
    "Upload",
    "among",
    "is_upload",
    "named_records",
    "referred",
]
