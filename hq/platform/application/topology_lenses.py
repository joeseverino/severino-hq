"""The standing questions asked of a topology, and the traces that follow one node's relationships."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timestamps import moment
from .topology_model import JOINED_KINDS, Topology, TopologyTrace

TRACE_DIRECTIONS = ("inbound", "outbound", "both")
MAX_TRACE_DEPTH = 5


@dataclass(frozen=True, slots=True)
class TopologyLens:
    """A standing question about the graph, answered from the graph itself.

    A lens owns no inventory and runs no query. It selects ids out of a
    projection already derived and already authorized, so a lens can only ever
    narrow what a principal sees: never widen it.
    """

    name: str
    label: str
    summary: str
    select: Callable[[Topology], frozenset[str]]


_ATTENTION_STATES = frozenset({"attention", "serious"})

# How far behind its own kind's latest observation a node may fall before the
# gap means it was skipped rather than swept a moment later. Sweeps write one
# timestamp for everything they confirm, so siblings land together.
_STALE_AFTER = timedelta(hours=1)


def _incoming_kinds(topology: Topology) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = {}
    for edge in topology.edges:
        incoming.setdefault(edge.target, set()).add(edge.kind)
    return incoming


def _without_inbound(topology: Topology, kind: str, edge_kind: str) -> frozenset[str]:
    """Nodes of one kind that nothing currently relates to in one way.

    The absence is the finding: a resource nothing observes and a resource
    nothing governs are different gaps, and neither is visible from a node in
    isolation.
    """

    incoming = _incoming_kinds(topology)
    return frozenset(
        node.id
        for node in topology.nodes
        if node.kind == kind and edge_kind not in incoming.get(node.id, frozenset())
    )


def _needs_attention(topology: Topology) -> frozenset[str]:
    return frozenset(
        node.id for node in topology.nodes if node.status in _ATTENTION_STATES
    )


def _unobserved_resources(topology: Topology) -> frozenset[str]:
    return _without_inbound(topology, "resource", "used_by")


def _ungoverned_resources(topology: Topology) -> frozenset[str]:
    return _without_inbound(topology, "resource", "governs")


def _unobserved_abilities(topology: Topology) -> frozenset[str]:
    return _without_inbound(topology, "ability", "enables")


def _unresolved_dependencies(topology: Topology) -> frozenset[str]:
    return frozenset(node.id for node in topology.nodes if node.kind == "dependency")


def _stale_observations(topology: Topology) -> frozenset[str]:
    """Nodes a sweep passed over while it confirmed their siblings.

    Compared against the newest observation of the same ``kind_key`` rather
    than the clock. A kind on a slower cadence is not stale, it is slower, and
    an absolute threshold cannot tell those apart.
    """

    latest: dict[str, datetime] = {}
    seen: dict[str, datetime] = {}
    for node in topology.nodes:
        if not node.observed_at or not node.kind_key or node.kind in JOINED_KINDS:
            continue
        observed = moment(node.observed_at, naive="keep")
        if observed is None:
            continue
        seen[node.id] = observed
        newest = latest.get(node.kind_key)
        if newest is None or observed > newest:
            latest[node.kind_key] = observed
    return frozenset(
        node.id
        for node in topology.nodes
        if node.id in seen and latest[node.kind_key] - seen[node.id] > _STALE_AFTER
    )


def _isolated(topology: Topology) -> frozenset[str]:
    related: set[str] = set()
    for edge in topology.edges:
        related.add(edge.source)
        related.add(edge.target)
    return frozenset(node.id for node in topology.nodes if node.id not in related)


# Derived from node kinds and edge kinds alone, so an extension that emits a
# resource or an ability answers them without knowing they exist. Nothing here
# names a domain, a provider, or an installed package.
TOPOLOGY_LENSES: tuple[TopologyLens, ...] = (
    TopologyLens("attention", "Has a problem",
        "Everything with a problem right now.",
        _needs_attention),
    TopologyLens("unobserved-resources", "In HQ but not found by any connection",
        "Records HQ keeps that no connection reports using.",
        _unobserved_resources),
    TopologyLens("ungoverned-resources", "In HQ but no connection can change it",
        "Records HQ keeps that none of its connections can change.",
        _ungoverned_resources),
    TopologyLens("unobserved-abilities", "Things HQ could read but currently cannot",
        "What a connection type offers that no working connection gives HQ now.",
        _unobserved_abilities),
    TopologyLens("unresolved-dependencies", "Reached by a connection but not in HQ",
        "Things a connection uses that HQ keeps no record of.",
        _unresolved_dependencies),
    TopologyLens("stale-observations", "Not read as recently as the rest",
        "Things last read well before others of the same type.",
        _stale_observations),
    TopologyLens("isolated", "Connected to nothing",
        "Things with no link to anything else.",
        _isolated),
)

_LENS_BY_NAME = {lens.name: lens for lens in TOPOLOGY_LENSES}


def topology_lenses() -> tuple[TopologyLens, ...]:
    """Every standing question any adapter may ask of the topology."""

    return TOPOLOGY_LENSES


def lens_for(name: str) -> TopologyLens | None:
    """Resolve a requested lens, or ``None`` when no declaration claims it."""

    return _LENS_BY_NAME.get(name)


def apply_lens(topology: Topology, lens: TopologyLens) -> Topology:
    """Narrow a derived projection to one lens, keeping only surviving edges.

    An edge whose other end the lens excluded is dropped rather than left
    dangling: every adapter resolves an edge's endpoints against the node set.
    """

    selected = lens.select(topology)
    return Topology(
        tuple(node for node in topology.nodes if node.id in selected),
        tuple(
            edge
            for edge in topology.edges
            if edge.source in selected and edge.target in selected
        ),
    )


def apply_trace(
    topology: Topology,
    focus: str,
    *,
    direction: str = "both",
    depth: int | str = 2,
) -> tuple[Topology, TopologyTrace | None]:
    """Select a bounded dependency neighborhood without deriving new state.

    ``outbound`` follows the graph's declared source-to-target direction;
    ``inbound`` answers what points at the focus. Unknown inputs deliberately
    leave the projection unchanged and report no applied trace, matching the
    standing-lens contract used by every delivery adapter.
    """

    node_ids = {node.id for node in topology.nodes}
    if focus not in node_ids:
        return topology, None
    selected_direction = direction if direction in TRACE_DIRECTIONS else "both"
    try:
        selected_depth = int(depth)
    except (TypeError, ValueError):
        selected_depth = 2
    selected_depth = min(max(selected_depth, 1), MAX_TRACE_DEPTH)

    adjacent: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in topology.edges:
        if selected_direction in ("outbound", "both"):
            adjacent[edge.source].add(edge.target)
        if selected_direction in ("inbound", "both"):
            adjacent[edge.target].add(edge.source)

    hops = {focus: 0}
    frontier = {focus}
    for hop in range(1, selected_depth + 1):
        frontier = {
            neighbor
            for node_id in frontier
            for neighbor in adjacent[node_id]
            if neighbor not in hops
        }
        if not frontier:
            break
        hops.update(dict.fromkeys(frontier, hop))

    narrowed = Topology(
        tuple(node for node in topology.nodes if node.id in hops),
        tuple(
            edge
            for edge in topology.edges
            if edge.source in hops and edge.target in hops
        ),
    )
    trace = TopologyTrace(
        focus=focus,
        direction=selected_direction,
        depth=selected_depth,
        hops=tuple(sorted(hops.items(), key=lambda item: (item[1], item[0]))),
    )
    return narrowed, trace


# Lanes the whole map leaves out until asked: what a credential could read,
# public lookups, an extension's accounts and items, and names nothing in HQ
# answers for. They are context for the lanes that stay, never the question.
QUIET_KINDS = frozenset({"ability", "registry", "target", "dependency"})


def kept_on_the_map(kind: str, degree: int) -> bool:
    """Whether the whole map shows a card before "show everything": its lane
    is not a quiet one, and something links to it."""

    return kind not in QUIET_KINDS and degree > 0
