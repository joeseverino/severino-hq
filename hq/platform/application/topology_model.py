"""The topology's vocabulary: nodes, the edges between them, and a trace.

Also how each relation ranks on a page, and the one way an edge is emitted, so
every module that adds edges gives them the same stable id and phrase.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated

from hq.domains.control_plane.providers import PROVIDERS

from .action_links import ActionLink as TopologyAction
from .answer_fields import ObservedAt
from .describe import Describe, listed
from .entity_links import EntityLink
from .timestamps import moment
from .ui import ended


@dataclass(frozen=True, slots=True)
class RelationKind:
    """What an edge kind says from each end, and where it ranks on a page.

    ``phrase`` reads from the source, ``inverse`` from the target. A lower
    ``rank`` is shown first.
    """

    phrase: str
    inverse: str
    rank: int


# Every structural edge kind, stated once. Reading edges take their phrase from
# the reading's ``relation`` and their rank from its facet.
RELATIONS: dict[str, RelationKind] = {
    "runs_on": RelationKind("Runs on", "Serves", 10),
    "runs": RelationKind("Runs", "Runs on", 15),
    "talks_to": RelationKind("Talks to", "Talks to", 20),
    "contains": RelationKind("Names", "In domain", 30),
    "redirects_to": RelationKind("Redirects to", "Redirected from", 25),
    "reaches": RelationKind("Reads", "Read through", 70),
    # A target a connection names as an estate node, kept apart from it by
    # its gate: the inverse is the fact a folded target leaves on that node.
    "stands_for": RelationKind("Stands for", "Reached as", 71),
    "on_tailnet": RelationKind("Tailnet device", "Machine", 75),
    "declared_by": RelationKind("Record in HQ", "For", 80),
    "carries": RelationKind("Holds the connections", "Held on", 85),
    "used_by": RelationKind("Reads", "Read through", 85),
    "enables": RelationKind("Can read", "Read through", 85),
    "governs": RelationKind("Can change", "Changed through", 85),
    "reading": RelationKind("", "Reads", 88),
}

# Where a reading's relation ranks, by the facet it supplies. What serves a
# name comes first; a protective overlay with no facet (Access) comes last.
READING_RANKS: dict[str, int] = {
    "runtime": 20,
    "network": 20,
    "dns": 35,
    "proxy": 40,
    "certificate": 50,
    "registration": 60,
    "": 90,
}

# Every node kind, in the order a page lists them.
NODE_KIND_ORDER = {
    "controller": 0,
    "connection": 1,
    "machine": 2,
    "service": 3,
    "zone": 4,
    "ability": 5,
    "resource": 6,
    "registry": 7,
    "target": 8,
    "dependency": 9,
}

# How a node, an edge or a finding stands, most severe first.
STANDINGS = ("serious", "attention", "neutral", "good")


@dataclass(frozen=True, slots=True)
class TopologyNode:
    """One addressable thing in the derived topology."""

    id: Annotated[
        str,
        Describe(
            "Names this node for the rest of the answer and later reads: an edge's source and target, "
            "a trace's focus, a problem's subject. Stable while the thing it stands for exists."
        ),
    ]
    kind: Annotated[str, Describe(ended(f"What sort of thing it is: {listed(NODE_KIND_ORDER)}"))]
    label: Annotated[
        str,
        Describe(
            "The key HQ files it under, which a command or a record's page is addressed by: "
            "a record's key, a machine's name, a hostname."
        ),
    ]
    subtitle: Annotated[str, Describe("What it is, in a few words under its name, such as the service it reads.")]
    name: Annotated[
        str,
        Describe(
            "What a person calls it, where that differs from `label`: the name a record stands for. "
            "Empty when `label` already reads as that name."
        ),
    ] = ""
    summary: Annotated[
        str,
        Describe(
            "What the record holds, shortened, when another record goes by the same name: "
            "what tells the two apart. Empty otherwise."
        ),
    ] = ""
    status: Annotated[str, Describe(ended(f"How it stands: {listed(STANDINGS)}"))] = "neutral"
    status_label: Annotated[str, Describe("How it stands, in the words a page shows beside it.")] = ""
    detail: Annotated[str, Describe("A line on why it stands as it does; empty when there is nothing to add.")] = ""
    url: Annotated[
        str,
        Describe("Where it is shown: a path in HQ, or an outside address such as a service's console. Empty for none."),
    ] = ""
    # The subtitle already reads as this, but a subtitle is a rendered label
    # and must never become a join key; grouping siblings needs identity.
    kind_key: Annotated[
        str,
        Describe(
            "What it is an instance of: a record's type, or the connection family that added it. "
            "Nodes alike in this are siblings."
        ),
    ] = ""
    provider: Annotated[
        str,
        Describe(
            "The service a connection node reads, such as `tailscale`, which its readings are matched by; "
            "`subtitle` names it in words. Empty on any other node."
        ),
    ] = ""
    connection_ref: Annotated[
        str,
        Describe("A connection node's ref, unique only within the controller that reported it. Empty otherwise."),
    ] = ""
    controller_id: Annotated[
        str,
        Describe(
            "The controller that reported a connection node; with `connection_ref` it names the connection. "
            "Empty otherwise."
        ),
    ] = ""
    # "" when nothing reads it. Health describes the content of the last
    # observation and says nothing about its age, so a thing observed once
    # and never again reads healthy forever. This is the fact that
    # distinguishes the two.
    observed_at: ObservedAt = ""
    # What was asked for, and what was last confirmed back. Between them the
    # whole of triage: equal means a reconcile already ran against this exact
    # declaration, so a difference the world still shows is the declaration
    # being wrong rather than the convergence being late.
    declared_revision: Annotated[
        int,
        Describe(
            "Which version of HQ's copy of the record HQ last asked for, counting up from 1 as the copy "
            "changes; 0 on a node HQ keeps no copy of."
        ),
    ] = 0
    observed_revision: Annotated[
        int,
        Describe(
            "Which version of HQ's copy was last applied and confirmed back, 0 until one has been. "
            "When it equals `declared_revision`, a difference the live system still shows lies in HQ's copy."
        ),
    ] = 0
    reason: Annotated[
        str,
        Describe(
            "The reason on its current condition, verbatim, saying whether a read of the live system "
            "wrote it or applying HQ's copy did, which no read has confirmed since."
        ),
    ] = ""
    # A disabled declaration is not a finding: nobody asked for it to be true.
    managed: Annotated[
        bool,
        Describe("Whether HQ keeps the live system to its copy; false, a difference is no problem."),
    ] = True
    on_demand: Annotated[
        bool,
        Describe("Whether it is expected to be absent most of the time, so a read not finding it is no problem."),
    ] = False
    # Drift is compared only across fields present in both, so a field the
    # reading omits is unverified rather than agreed.
    unconfirmed_fields: Annotated[
        tuple[str, ...],
        Describe(
            "Fields HQ's copy sets that the last read did not report back, leaving out those the service "
            "cannot report: set, but never checked."
        ),
    ] = ()
    # A findings rule derives from this topology and is not allowed a query of
    # its own (the suite measures that) so an observation a rule has to reason
    # about has to arrive here or not at all.
    #
    # Flat and small on purpose. This is not a second copy of the inventory; it
    # is the handful of observed facts that decide something.
    facts: Annotated[
        tuple[tuple[str, str], ...],
        Describe("What a read found that HQ's copy does not carry, as `[name, value]` pairs; a name may repeat."),
    ] = ()
    # Nobody visited and nobody looked are opposite findings, and the second is
    # the one worth acting on: a target HQ reaches, and nothing measures, is a
    # site running unobserved.
    pageviews: Annotated[
        int | None,
        Describe("Pages served under this hostname over the last 7 days; null, never 0, when nothing measures it."),
    ] = None
    visits: Annotated[
        int | None,
        Describe("Visits to this hostname over the last 7 days; null when nothing measures them."),
    ] = None
    actions: Annotated[
        tuple[TopologyAction, ...],
        Describe("What the reader may do from here, each already checked against the reader's permissions."),
    ] = ()
    # A claim about it reaches only such a reader, wherever it is derived.
    read_capabilities: Annotated[
        tuple[str, ...],
        Describe(
            "The permissions a reader must hold to be told about it: for what a service reported, "
            "its connection family's own."
        ),
    ] = ()


@dataclass(frozen=True, slots=True)
class TopologyEdge:
    """A relationship derived from a declaration or observation."""

    id: Annotated[str, Describe("The relationship's id, stable while both ends and the relation hold.")]
    source: Annotated[str, Describe("The id of the node it reads from.")]
    target: Annotated[str, Describe("The id of the node it points to.")]
    kind: Annotated[str, Describe(f"The relation: {listed(RELATIONS)}, the last for one a reading supports.")]
    label: Annotated[str, Describe("The relation as it reads from the source, such as `Runs on`.")]
    status: Annotated[str, Describe(ended(f"How the relationship stands: {listed(STANDINGS)}"))] = "neutral"
    # An edge a reading supports exists only while its reading does.
    source_kind: Annotated[
        str,
        Describe("The type of reading the relationship rests on; empty for one HQ's records state."),
    ] = ""
    detail: Annotated[str, Describe("What its reading names, in a few words; empty without a reading.")] = ""
    observed_at: ObservedAt = ""
    entities: Annotated[
        tuple[EntityLink, ...],
        Describe("Each record its reading stands for, as a link; empty without a reading."),
    ] = ()
    facet: Annotated[
        str,
        Describe(f"What its reading supplies: {listed(READING_RANKS)}; empty for an overlay that supplies none."),
    ] = ""


def relation_rank(edge: TopologyEdge) -> int:
    """Where an edge's relation is shown among a node's relationships."""

    if edge.kind == "reading":
        return READING_RANKS.get(edge.facet, READING_RANKS[""])
    relation = RELATIONS.get(edge.kind)
    return relation.rank if relation else 99


@dataclass(frozen=True, slots=True)
class Topology:
    """The complete permitted projection consumed by web, API, and MCP."""

    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]
    # Checks of HQ's standard read and found failing, as ``(id, evidence,
    # read_capabilities)``, here so the posture finding rule costs no query.
    unmet_standard: tuple[tuple[str, str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class TopologyTrace:
    """A bounded traversal applied to an already-authorized topology."""

    focus: str
    direction: str
    depth: int
    hops: tuple[tuple[str, int], ...]


# Node kinds whose ``observed_at`` is the newest of the readings joined to
# them rather than one sweep's stamp, so siblings are not compared by it.
JOINED_KINDS = frozenset({"machine", "service", "zone", "registry", "controller"})

# Node kinds a sweep or a reading can observe. A declaration is observable
# unless its provider says no sweep reads it.
_OBSERVABLE_KINDS = frozenset({"connection", *JOINED_KINDS})


def observable(node: TopologyNode) -> bool:
    """Whether anything can observe this node, so "never observed" is a gap."""

    if node.kind == "resource":
        provider = PROVIDERS.get(node.kind_key)
        return not (provider and provider.unobserved_reason)
    return node.kind in _OBSERVABLE_KINDS


def newest_stamp(*stamps: str) -> str:
    """The latest of several ISO 8601 instants, or ""."""

    moments = [when for stamp in stamps if (when := moment(stamp)) is not None]
    return max(moments).isoformat() if moments else ""


def derived_id(kind: str, *parts: str) -> str:
    """Keep composite observation ids stable and compact, not confidential."""

    digest = sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


# Every reader of the topology holds it, so a gate is compared above it.
_READ_FLOOR = frozenset({"read"})


def gate_covers(holder: tuple[str, ...], gate: tuple[str, ...]) -> bool:
    """Whether a reader ``holder``'s gate admits may be told what ``gate`` guards."""

    return set(gate) - _READ_FLOOR <= set(holder) - _READ_FLOOR


def edge_between(source: str, target: str, kind: str, label: str = "", status: str = "neutral") -> TopologyEdge:
    """The edge of one relation kind from ``source`` to ``target``, with a stable id."""

    return TopologyEdge(
        id=derived_id("edge", source, target, kind),
        source=source,
        target=target,
        kind=kind,
        label=label or RELATIONS[kind].phrase,
        status=status,
    )
