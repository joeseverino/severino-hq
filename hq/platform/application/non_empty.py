"""The one way HQ says a value must hold something: ``NonEmpty``, and
``RequiredText`` for text.

A leaf of its own, importing nothing of HQ and no validation library at load,
so a contract that must stay a leaf (``automation_manifest``) holds its fields
to it too. ``command_fields`` re-exports both, beside the other markers.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

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
