"""What a command's field says about itself beyond its type.

A field carries these markers in its ``Annotated`` type, and
``integration_specs.command_schema`` writes them into the field's JSON Schema,
the one shape every adapter reads: the command page shows a description as
the field's help and offers a record-naming field's records, a reference's
things, or a field's declared choices, to choose from, and the machine
contract publishes each. They are plain values rather than pydantic's
``Field``, so a module that declares a command loads no validation library at
startup; pydantic leaves metadata it does not know alone.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from django.db.models import Model

# The JSON Schema extension a record-naming field carries.
NAMES_RECORDS = "x-hq-records"
# The JSON Schema extension a field holding a model's reference carries.
REFERS = "x-hq-reference"


@dataclass(frozen=True, slots=True)
class Describe:
    """What a field is, in a sentence a person reads beside it:
    ``notes: Annotated[str, Describe("Anything else worth keeping.")]``."""

    text: str


@dataclass(frozen=True, slots=True)
class Names:
    """A field whose value is one of ``resource``'s records (several, as a list).

    ``key`` is the item field the value is, where that is not the resource's
    identifier:
    ``project: Annotated[str, Names("projects"), Describe("The project it is for.")]``.
    """

    resource: str
    key: str = ""


@dataclass(frozen=True, slots=True, init=False)
class Choices:
    """A field whose value is one of a fixed set declared elsewhere, each with
    the words a person reads for it: ``Choices(Appearance.Theme.choices)``.
    The form offers only these; the handler refuses any other on every path."""

    options: tuple[tuple[str, str], ...]

    def __init__(self, options: Iterable[tuple[Any, Any]]) -> None:
        object.__setattr__(self, "options", tuple((str(value), str(label)) for value, label in options))


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


def _declared(metadata: tuple[Any, ...]) -> dict[str, Any]:
    declared: dict[str, Any] = {}
    for marker in metadata:
        if isinstance(marker, Describe):
            declared["description"] = marker.text
        elif isinstance(marker, Names):
            declared[NAMES_RECORDS] = {"resource": marker.resource, **({"key": marker.key} if marker.key else {})}
        elif isinstance(marker, Refers):
            declared[REFERS] = {"model": marker.model, "field": marker.field}
        elif isinstance(marker, Choices):
            declared["oneOf"] = [{"const": value, "title": label} for value, label in marker.options]
    return declared


def marked(command_type: type) -> dict[str, dict[str, Any]]:
    """What each field of ``command_type`` declares through its markers, as
    the schema keys to write into that field's property."""

    hints = get_type_hints(command_type, include_extras=True)
    return {
        name: declared
        for name, hint in hints.items()
        if get_origin(hint) is Annotated and (declared := _declared(get_args(hint)[1:]))
    }


def named_records(schema: dict[str, Any]) -> dict[str, Any] | None:
    """The records one field's schema names, or ``None``."""

    declared = schema.get(NAMES_RECORDS)
    return declared if isinstance(declared, dict) else None


def referred(schema: dict[str, Any]) -> tuple[str, str] | None:
    """``(model label, field)`` of the reference one field's schema holds, or ``None``."""

    declared = schema.get(REFERS)
    return (declared["model"], declared["field"]) if isinstance(declared, dict) else None
