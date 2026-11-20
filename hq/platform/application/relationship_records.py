"""The shape the relationships resource answers in: one node's edges and readings."""

from typing import Annotated, Any, TypedDict

from .answer_fields import AsRead
from .describe import Describe
from .json_schemas import Written
from .topology_model import RelationGroup


class JoinedReadingPart(TypedDict):
    kind: Annotated[str, Describe("The reading's type, such as `adguard.client`.")]
    label: Annotated[str, Describe("The reading's name, as a page heads it.")]
    connection: Annotated[str, Describe("The connection it was read through; empty for none.")]
    records: Annotated[list[AsRead], Describe("The records it holds about the node, as its type's schema admits them.")]


class RelationshipsRecord(TypedDict):
    """One node's edges, grouped by what each says from it, and the readings that hold it."""

    node: Annotated[str, Describe("The id of the node asked about, such as `machine:<name>` or `zone:<domain>`.")]
    focus_url: Annotated[str, Describe("The map centered on the node.")]
    groups: Annotated[
        list[Annotated[dict[str, Any], Written(RelationGroup)]],
        Describe("Its relationships, grouped by what each says from the node."),
    ]
    readings: Annotated[list[JoinedReadingPart], Describe("The readings joined to the node.")]
    unreadable: Annotated[list[str], Describe("The readings about the node that could not be read, in words.")]
