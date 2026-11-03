"""What a field is, in a sentence, carried in its ``Annotated`` type.

A leaf of its own, importing nothing of HQ, so a contract that must stay a
leaf (``workflow_contracts``) describes its fields too. The other markers live
in ``command_fields``, which re-exports this one.

The text is read beside the field in the API reference, so under
``hq/platform/application`` ``lint:prose`` holds it to the words a page may
show; a closed set is listed from its constant rather than written out.
"""

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
