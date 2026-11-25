"""The one way HQ says a value must hold something: ``NonEmpty``, and
``RequiredText`` for text, held by pydantic where a schema validates it and by
``holds_something`` where a plain dataclass is built.

A leaf of its own, importing nothing of HQ and no validation library at load,
so a contract that must stay a leaf (``automation_manifest``) holds its fields
to it too. ``command_fields`` re-exports both, beside the other markers.
"""

from dataclasses import dataclass, fields
from functools import cache
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Union, cast, get_args, get_origin

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema


@dataclass(frozen=True, slots=True)
class NonEmpty:
    """A text, collection or object that holds at least one character or item,
    so an empty one is refused as the field is validated and its schema says
    ``minLength`` (``minItems``, ``minProperties``)."""

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        schema = handler(source)
        if schema["type"] not in {"str", "list", "tuple", "set", "frozenset", "dict"}:
            raise TypeError(f"NonEmpty holds a text, a collection or an object, never a {schema['type']}.")
        return cast("CoreSchema", {**schema, "min_length": 1})


# Text a field must be sent with something in: a name, a key, a reference.
RequiredText = Annotated[str, NonEmpty()]


def holds_something(view: Any) -> None:
    """Refuse ``view``, a dataclass, where a field its annotation holds to
    ``NonEmpty`` is empty, or for text, blank: what a person reads is never
    whitespace alone."""

    for name in _held(type(view)):
        value = getattr(view, name)
        if (isinstance(value, str) and not value.strip()) or (hasattr(value, "__len__") and not len(value)):
            raise ValueError(f"{type(view).__name__}.{name} must hold something; got {value!r}.")


@cache
def _held(kind: type) -> tuple[str, ...]:
    return tuple(field.name for field in fields(kind) if _non_empty(field.type))


def _non_empty(hint: Any) -> bool:
    """Whether ``hint`` is held to ``NonEmpty``, alone or as an option of a union."""

    if get_origin(hint) is Annotated:
        return any(isinstance(mark, NonEmpty) for mark in hint.__metadata__)
    if isinstance(hint, UnionType) or get_origin(hint) is Union:
        return any(_non_empty(option) for option in get_args(hint))
    return False
