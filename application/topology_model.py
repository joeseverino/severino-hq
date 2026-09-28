"""The topology's vocabulary: nodes, the edges between them, and a trace.

Also how each relation ranks on a page, and the one way an edge is emitted, so
every module that adds edges gives them the same stable id and phrase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from control_plane.providers import PROVIDERS

from .action_links import ActionLink as TopologyAction
from .entity_links import EntityLink


@dataclass(frozen=True)
class TopologyNode:
    """One addressable thing in the derived topology."""

    id: str
    kind: str
    label: str
    subtitle: str
    status: str = "neutral"
    status_label: str = ""
    detail: str = ""
    url: str = ""
    # What this node is an instance of: a provider kind, or the connection
    # family that emitted it. The subtitle already reads as this, but a subtitle
    # is a rendered label and must never become a join key; grouping siblings
    # needs identity.
    kind_key: str = ""
    # A connection node's provider (``tailscale``, ``cloudflare_api``): the join
    # key its readings are matched by. The subtitle is that provider's label.
    provider: str = ""
    # A connection node's ref and the controller that reported it: the join keys
    # for facts about the connection. A ref is unique per controller only.
    connection_ref: str = ""
    controller_id: str = ""
    # When this was last observed, ISO 8601, or "" when nothing observes it.
    # Health describes the content of the last observation and says nothing
    # about its age, so a thing observed once and never again reads healthy
    # forever. This is the fact that distinguishes the two.
    observed_at: str = ""
    # What was asked for, and what was last confirmed back. Between them the
    # whole of triage: equal means a reconcile already ran against this exact
    # declaration, so a difference the world still shows is the declaration
    # being wrong rather than the convergence being late.
    declared_revision: int = 0
    observed_revision: int = 0
    # The reason on the active condition, verbatim. "Observed" was written by a
    # sweep that saw this; "Reconciled" was written by a reconcile and says
    # nothing about whether a sweep has confirmed it since.
    reason: str = ""
    # Whether HQ is converging this at all. A disabled declaration is not a
    # finding: nobody asked for it to be true.
    managed: bool = True
    # Declared as usually absent, so a sweep not finding it is expected.
    on_demand: bool = False
    # Fields this declaration asserts that the last observation did not echo
    # back, excluding the ones the provider declared it cannot report. Drift is
    # compared only across fields present in both, so a field the reading omits
    # is unverified rather than agreed: the difference between "we set this"
    # and "we checked this".
    unconfirmed_fields: tuple[str, ...] = ()
    # Facts a sweep reports that no declaration carries, as flat strings. A
    # findings rule derives from this topology and is not allowed a query of its
    # own (the suite measures that) so an observation a rule has to reason
    # about has to arrive here or not at all.
    #
    # Flat and small on purpose. This is not a second copy of the inventory; it
    # is the handful of observed facts that decide something.
    facts: tuple[tuple[str, str], ...] = ()
    # What this name actually served, where anything measures it. ``None`` is
    # not zero: nobody visited and nobody looked are opposite findings, and the
    # second is the one worth acting on: a target HQ reaches, and nothing
    # measures, is a site running unobserved.
    pageviews: int | None = None
    visits: int | None = None
    actions: tuple[TopologyAction, ...] = ()


@dataclass(frozen=True)
class TopologyEdge:
    """A relationship derived from a declaration or observation."""

    id: str
    source: str
    target: str
    kind: str
    label: str
    status: str = "neutral"
    # For an edge a reading supports: the reading kind, what it names, and
    # when it was read. An edge exists only while its reading does.
    source_kind: str = ""
    detail: str = ""
    observed_at: str = ""
    # For a reading edge: each record it stands for, through the link builder,
    # and the facet the reading supplies.
    entities: tuple[EntityLink, ...] = ()
    facet: str = ""


@dataclass(frozen=True)
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
    "contains": RelationKind("Contains", "In domain", 30),
    "redirects_to": RelationKind("Redirects to", "Redirected from", 25),
    "reaches": RelationKind("Reaches", "Reached through", 70),
    "on_tailnet": RelationKind("On the tailnet as", "Tailnet device of", 75),
    "declared_by": RelationKind("Declared by", "Declares", 80),
    "carries": RelationKind("Carries", "Carried by", 85),
    "used_by": RelationKind("Used by", "Uses", 85),
    "enables": RelationKind("Enables", "Enabled by", 85),
    "governs": RelationKind("Governs", "Governed by", 85),
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


def relation_rank(edge: "TopologyEdge") -> int:
    """Where an edge's relation is shown among a node's relationships."""

    if edge.kind == "reading":
        return READING_RANKS.get(edge.facet, READING_RANKS[""])
    relation = RELATIONS.get(edge.kind)
    return relation.rank if relation else 99


@dataclass(frozen=True)
class Topology:
    """The complete permitted projection consumed by web, API, and MCP."""

    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]


@dataclass(frozen=True)
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

    moments = []
    for stamp in stamps:
        try:
            moment = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            continue
        if moment is not None:
            moments.append(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    return max(moments).isoformat() if moments else ""


def derived_id(kind: str, *parts: str) -> str:
    """Keep composite observation ids stable and compact, not confidential."""

    digest = sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


def edge_between(
    source: str, target: str, kind: str, label: str = "", status: str = "neutral"
) -> TopologyEdge:
    """The edge of one relation kind from ``source`` to ``target``, with a stable id."""

    return TopologyEdge(
        id=derived_id("edge", source, target, kind),
        source=source,
        target=target,
        kind=kind,
        label=label or RELATIONS[kind].phrase,
        status=status,
    )
