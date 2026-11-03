"""What the topology read answers, typed apart from the graph that derives it,
so a door declares it without loading the graph."""

from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import WithJsonSchema

from .command_fields import Describe
from .json_schemas import Written
from .projection import shape_version
from .topology_lenses import TRACE_DIRECTIONS
from .topology_model import TopologyEdge, TopologyNode, one_of

# The shape topology answers in; it moves when that shape does.
TOPOLOGY_SCHEMA_VERSION = 2


class TraceHop(TypedDict):
    """One node a trace reached, and how far from its focus."""

    node: Annotated[str, Describe("The id of a node the trace reached, as `nodes` lists it.")]
    hop: Annotated[int, Describe("How many edges it lies from the focus, which is 0.")]


class TraceAnswer(TypedDict):
    """A trace from one node: which way it followed the edges, how far, and each node it reached."""

    focus: Annotated[str, Describe("The id of the node the trace started from.")]
    direction: Annotated[
        str,
        Describe(
            f"Which way it followed the edges, {one_of(TRACE_DIRECTIONS)}: `outbound` goes from source to "
            "target, toward what the focus depends on, and `inbound` goes back, toward what depends on it."
        ),
    ]
    depth: Annotated[int, Describe("The most edges it followed from the focus.")]
    hops: Annotated[list[TraceHop], Describe("Every node it reached, nearest first.")]


class TopologySummary(TypedDict):
    """How much the graph holds."""

    nodes: Annotated[int, Describe("How many nodes the graph holds for this reader.")]
    edges: Annotated[int, Describe("How many relationships the graph holds for this reader.")]
    kinds: Annotated[dict[str, int], Describe("How many nodes of each type, by type.")]


class LensEntry(TypedDict):
    """A fixed question the graph can be narrowed to."""

    name: Annotated[str, Describe("What to pass as `lens` to ask it.")]
    label: Annotated[str, Describe("The question, in a few words.")]
    summary: Annotated[str, Describe("What its answer holds, in a sentence.")]


class TopologyAnswer(TypedDict):
    """What ``topology`` answers: the graph a lens or a trace narrows to, or,
    asked for neither, its counts and the lenses that would narrow it."""

    schema_version: Annotated[int, WithJsonSchema(shape_version(TOPOLOGY_SCHEMA_VERSION))]
    lens: Annotated[str | None, Describe("The lens the graph was narrowed to; null when none was asked for.")]
    trace: Annotated[TraceAnswer | None, Describe("The trace the graph was narrowed to; null without a focus.")]
    summary: TopologySummary
    lenses: NotRequired[
        Annotated[list[LensEntry], Describe("Every lens there is, given only when neither a lens nor a focus was.")]
    ]
    nodes: NotRequired[
        Annotated[
            list[Annotated[dict[str, Any], Written(TopologyNode)]],
            Describe("The things a lens or a trace selected; absent when neither was asked for."),
        ]
    ]
    edges: NotRequired[
        Annotated[
            list[Annotated[dict[str, Any], Written(TopologyEdge)]],
            Describe("The relationships between the selected nodes, both ends among them."),
        ]
    ]
