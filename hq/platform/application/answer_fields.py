"""The fields several answers carry with one meaning, each said once.

Each is a named type alias, so the API document writes its definition once
under the alias's name and each use as a reference to it (``json_schemas``).
A field whose meaning differs by type (a ``status``, a ``label``, an ``id``)
is declared with its type, where it means what it says. Like ``describe``,
it is a leaf that loads neither HQ nor pydantic at import, so a leaf contract
(``workflow_contracts``) can use it too.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from .describe import Describe

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic_core import CoreSchema


@dataclass(frozen=True, slots=True)
class Pinned:
    """A use of an alias that holds one value of it: ``Annotated[SchemaVersion,
    Pinned(2)]`` is the alias's reference with ``const`` beside it, so the
    alias says what the field is and the use says which value it always has."""

    value: Any

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), "const": self.value}


type Outcome = Annotated[bool, Describe("Whether HQ did what was asked: true beside `data`, false beside `error`.")]
# Where nothing has read it yet a use says how, by its own type: ``ObservedAt |
# None`` for null, a default of "" for empty.
type ObservedAt = Annotated[str, Describe("When it was last read from its source, an ISO 8601 date-time.")]
type UpdatedAt = Annotated[str, Describe("When it last changed in HQ, an ISO 8601 date-time.")]
type SchemaVersion = Annotated[
    int,
    Describe("Which version of this answer's shape it is; it moves when a client must read the shape differently."),
]
# What every description of a list, a connection and a command says of who may use it.
type RequiredPermissions = Annotated[
    list[str], Describe("The permissions a caller must hold, every one of them, to use it.")
]
type Permitted = Annotated[
    bool,
    Describe(
        "Whether the caller holds every permission `required_capabilities` names; absent from a read that names no caller."
    ),
]
type WebRoute = Annotated[
    str | None,
    Describe("The page of HQ's web interface that shows it, by its route name such as `projects:list`; null for none."),
]
# A record's fields that projects, assets and content share.
type RecordSlug = Annotated[
    str, Describe("Its address in HQ, unique among records of its type: what its page and its links name it by.")
]
# What an asset and an expense say of their cost alike.
type TotalCostAmount = Annotated[
    str, Describe("What it cost in all, a decimal string with two places such as `12.50`.")
]
type BusinessUseShare = Annotated[
    int, Describe("How much of it is for the business, a whole percentage from 0 to 100.")
]
type DeductibleAmount = Annotated[
    str,
    Describe(
        "An estimate of what may be deducted: `total_cost` times `business_use_percentage`, "
        "rounded to the cent, as a decimal string."
    ),
]
# The records a project and an asset are each linked to alike.
type LinkedDocs = Annotated[
    list[str], Describe("The vault doc IDs of the docs linked to it, sorted, leaving out any too sensitive to share.")
]
type LinkedContent = Annotated[list[str], Describe("The slugs of the writeups and pages linked to it, sorted.")]
type LinkedExpenses = Annotated[list[int], Describe("The ids of the expenses linked to it, newest first.")]
# An item of the action queue, which HQ's own problems are too.
type Since = Annotated[
    str,
    Describe("When the matter began, an ISO 8601 date-time, where its source knows; empty when it does not."),
]
