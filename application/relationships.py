"""What one node is related to, read off the relation graph.

A page about a machine, a service or a domain asks ``relationships_for`` with
the node's id. The answer is the node's edges from the topology's own relation
graph, in both directions, grouped by the phrase each edge says from where the
node stands and ordered by ``topology.relation_rank``. Nothing here derives a
relationship: an edge is either in the graph or not on the page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .entity_links import EntityLink, entity_link, node_link
from .facts import readings, unreadable_labels
from .security import Principal
from .topology import RELATIONS, TopologyEdge, relation_graph, relation_rank


@dataclass(frozen=True)
class Relationship:
    """One thing a node is related to, who says so, and when they last read it."""

    entity: EntityLink
    source: EntityLink | None = None
    observed_at: datetime | None = None
    stale: bool = False


@dataclass(frozen=True)
class RelationGroup:
    phrase: str
    rank: int
    items: tuple[Relationship, ...]


@dataclass(frozen=True)
class Relationships:
    node_id: str
    groups: tuple[RelationGroup, ...] = ()
    focus_url: str = ""
    # ``(label, connection, text)``: each source's raw records, for a disclosure.
    readouts: tuple[tuple[str, EntityLink | None, str], ...] = ()
    # Labels of kinds whose last read was refused.
    unreadable: tuple[str, ...] = ()
    # Whether the relation graph holds the node at all.
    known: bool = True

    def __bool__(self) -> bool:
        return bool(self.groups or self.unreadable)

    def phrases(self) -> tuple[str, ...]:
        return tuple(group.phrase for group in self.groups)

    def group(self, phrase: str) -> RelationGroup | None:
        return next((group for group in self.groups if group.phrase == phrase), None)

    def labels(self, phrase: str) -> tuple[str, ...]:
        found = self.group(phrase)
        return tuple(item.entity.label for item in found.items) if found else ()

    def without(self, *phrases: str) -> "Relationships":
        """The same answer less the groups a page already renders elsewhere."""

        return Relationships(
            node_id=self.node_id,
            groups=tuple(group for group in self.groups if group.phrase not in phrases),
            focus_url=self.focus_url,
            readouts=self.readouts,
            unreadable=self.unreadable,
            known=self.known,
        )


def _moment(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def _rows(edge: TopologyEdge, node_id: str, nodes: dict[str, Any]):
    """``(phrase, relationship)`` for each thing one edge relates the node to."""

    outbound = edge.source == node_id
    other = nodes[edge.target if outbound else edge.source]
    observed = _moment(edge.observed_at)
    stale = edge.status == "attention"
    if edge.kind == "reading":
        if outbound:
            yield RELATIONS["reading"].inverse, Relationship(node_link(other), None, observed, stale)
            return
        source = node_link(other)
        for entity in edge.entities or (EntityLink(edge.detail or edge.label),):
            yield edge.label, Relationship(entity, source, observed, stale)
        return
    relation = RELATIONS.get(edge.kind)
    if relation is None:
        phrase = edge.label
    else:
        phrase = relation.phrase if outbound else relation.inverse
    yield phrase, Relationship(node_link(other))


def relationships_for(node_id: str, *, principal: Principal) -> Relationships:
    """Every edge of one node, grouped by what it says from that node."""

    from .action_links import topology_url

    graph = relation_graph(principal=principal)
    nodes = {node.id: node for node in graph.topology.nodes}
    if node_id not in nodes:
        return Relationships(node_id=node_id, unreadable=unreadable_labels(), known=False)
    # An end whose page is this page (a zone's own declaration) says nothing here.
    own_url = node_link(nodes[node_id]).url
    grouped: dict[str, tuple[int, list[Relationship]]] = {}
    for edge in graph.topology.edges:
        if node_id not in (edge.source, edge.target) or edge.source == edge.target:
            continue
        rank = relation_rank(edge)
        for phrase, row in _rows(edge, node_id, nodes):
            if own_url and row.entity.url == own_url:
                continue
            current_rank, items = grouped.setdefault(phrase, (rank, []))
            if row not in items:
                items.append(row)
            grouped[phrase] = (min(current_rank, rank), items)
    groups = tuple(
        RelationGroup(
            phrase,
            rank,
            tuple(sorted(items, key=lambda row: row.entity.label.casefold())),
        )
        for phrase, (rank, items) in sorted(
            grouped.items(), key=lambda pair: (pair[1][0], pair[0])
        )
    )
    return Relationships(
        node_id=node_id,
        groups=groups,
        focus_url=topology_url(node_id),
        readouts=_readouts(graph.subjects.get(node_id)),
        unreadable=unreadable_labels(),
    )


def readout_records(subject) -> tuple[tuple[str, str, str, tuple[dict[str, Any], ...]], ...]:
    """``(kind, label, connection ref, records)`` for each source joined to a subject.

    The records are the stored, schema-filtered ones, once each.
    """

    if not subject:
        return ()
    found: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for joined in readings().about(subject):
        records = found.setdefault((joined.kind, joined.label, joined.connection_ref), [])
        record = joined.spec.admitted(joined.record)
        if record not in records:
            records.append(record)
    return tuple(
        (kind, label, ref, tuple(records)) for (kind, label, ref), records in found.items()
    )


def _readouts(subject) -> tuple[tuple[str, EntityLink | None, str], ...]:
    """Each source's schema-filtered records joined to the node, as JSON."""

    merged: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for _kind, label, ref, records in readout_records(subject):
        kept = merged.setdefault((label, ref), [])
        kept.extend(record for record in records if record not in kept)
    return tuple(
        (
            label,
            entity_link("connection", ref) if ref else None,
            json.dumps(records, indent=2, sort_keys=True),
        )
        for (label, ref), records in merged.items()
    )
