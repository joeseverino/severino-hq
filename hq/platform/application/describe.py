"""What a field is, in a sentence, carried in its ``Annotated`` type.

A leaf of its own, importing nothing of HQ, so a contract that must stay a
leaf (``workflow_contracts``) describes its fields too. The other markers live
in ``command_fields``, which re-exports this one.

The text is read beside the field in the API reference, so ``lint:prose``
holds it, as every string under ``hq/`` that can reach a page, to the words a
page may show; a closed set is named from its constant through ``listed``
rather than written out.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic_core import CoreSchema


@dataclass(frozen=True, slots=True)
class Describe:
    """What a field is, in a sentence a person reads beside it:
    ``notes: Annotated[str, Describe("Anything else worth keeping.")]``."""

    text: str

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), "description": self.text}


def listed(values: Iterable[str]) -> str:
    """A closed set's values as a description names them: ``a``, ``b`` or ``c``,
    leaving out an empty one."""

    named = [f"`{value}`" for value in values if value]
    return f"{', '.join(named[:-1])} or {named[-1]}" if len(named) > 1 else "".join(named)
