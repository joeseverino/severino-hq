"""What the topology read answers, typed apart from the graph that derives it,
so a door declares it without loading the graph."""

from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import WithJsonSchema

from .json_schemas import Written
from .projection import shape_version
from .topology_model import TopologyEdge, TopologyNode

# The shape topology answers in; it moves when that shape does.
TOPOLOGY_SCHEMA_VERSION = 2


class TraceHop(TypedDict):
    node: str
    hop: int


class TraceAnswer(TypedDict):
    """A trace from one node: which way it followed the edges, how far, and each node it reached."""

    focus: str
    direction: str
    depth: int
    hops: list[TraceHop]


class TopologySummary(TypedDict):
    nodes: int
    edges: int
    # How many nodes of each kind.
    kinds: dict[str, int]


class LensEntry(TypedDict):
    name: str
    label: str
    summary: str


class TopologyAnswer(TypedDict):
    """What ``topology`` answers: the graph a lens or a trace narrows to, or,
    asked for neither, its counts and the lenses that would narrow it."""

    schema_version: Annotated[int, WithJsonSchema(shape_version(TOPOLOGY_SCHEMA_VERSION))]
    lens: str | None
    trace: TraceAnswer | None
    summary: TopologySummary
    lenses: NotRequired[list[LensEntry]]
    nodes: NotRequired[list[Annotated[dict[str, Any], Written(TopologyNode)]]]
    edges: NotRequired[list[Annotated[dict[str, Any], Written(TopologyEdge)]]]
