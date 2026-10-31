"""The topology workspace and the drill-in for one node."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, override
from urllib.parse import urlencode

from django.http import Http404
from django.views.generic import TemplateView

from hq.platform.application.action_links import topology_url
from hq.platform.application.analytics import HOST_TRAFFIC_DAYS
from hq.platform.application.capabilities import capability_words
from hq.platform.application.entity_links import NODE_KINDS, node_link
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.application.timestamps import moment
from hq.platform.application.topology import derive_topology
from hq.platform.application.topology_lenses import (
    apply_lens,
    apply_trace,
    kept_on_the_map,
    lens_for,
    topology_lenses,
)
from hq.platform.application.topology_model import RELATIONS, TopologyTrace, observable, relation_rank
from hq.platform.application.ui import counted

# What a lane of the map is called, and what one card in it is counted as. A
# node kind with no entry here takes its noun from the node kind registry.
_LANE_NOUNS: dict[str, tuple[str, str, str]] = {
    "ability": ("What HQ can read", "thing HQ can read", "things HQ can read"),
    "resource": ("Records", "record", "records"),
    "registry": ("Lookups", "lookup", "lookups"),
    "target": ("Accounts and items", "account or item", "accounts and items"),
    "dependency": ("Not in HQ", "thing not in HQ", "things not in HQ"),
}

# The ways to follow links from one card, by the direction in the URL.
_TRACE_DIRECTIONS = (
    ("inbound", "What depends on this"),
    ("outbound", "What this depends on"),
    ("both", "Both"),
)


def _lane_nouns(kind: str) -> tuple[str, str, str]:
    """A lane's heading, and the singular and plural its cards are counted in."""

    if kind in _LANE_NOUNS:
        return _LANE_NOUNS[kind]
    known = NODE_KINDS[kind]
    return known.plural.capitalize(), known.noun, known.plural


def _lane(kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """One lane of the map: the heading names it, and a count says one or the
    other of its nouns."""

    heading, noun, plural = _lane_nouns(kind)
    return {"kind": kind, "label": heading, "count": counted(len(items), noun, plural), "items": items}


@dataclass(frozen=True, slots=True)
class _Lanes:
    """The map's cards by lane, how many were left out, and the focused one."""

    groups: tuple[dict[str, Any], ...]
    shown: int
    left_out: int
    # The focused node's body, drawn once in the panel below the lanes rather
    # than inside its card.
    detail: dict[str, Any] | None

    @classmethod
    def of(cls, items: list[dict[str, Any]], *, everything: bool, focus: str) -> _Lanes:
        drawn = [item for item in items if everything or kept_on_the_map(item["node"].kind, item["degree"])]
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in drawn:
            by_kind.setdefault(item["node"].kind, []).append(item)
        return cls(
            groups=tuple(_lane(kind, cards) for kind, cards in by_kind.items()),
            shown=len(drawn),
            left_out=len(items) - len(drawn),
            detail=next((item for item in drawn if focus and item["node"].id == focus), None),
        )


def _reset_url(lens_name: str) -> str:
    """The map with no trace, keeping the view it was seen through."""

    query = f"?{urlencode({'lens': lens_name})}" if lens_name else ""
    return f"{reverse('control_plane:topology')}{query}#map"


class TopologyView(PageMixin, TemplateView):
    """The live, actionable graph derived by the application layer."""

    template_name = "control_plane/topology.html"
    page_title = "Map"

    @override
    def get_page_actions(self):
        return (
            PageAction(
                capability_words("infrastructure.resource.create"), reverse("control_plane:create"), primary=True
            ),
            PageAction("All records", reverse("control_plane:list")),
            PageAction("Connections", reverse("control_plane:connections")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asked = self.request.GET
        active_lens = lens_for(asked.get("lens", "").strip())
        lens_name = active_lens.name if active_lens else ""
        topology = derive_topology(principal=web_principal(self.request.user), request=self.request)
        if active_lens is not None:
            topology = apply_lens(topology, active_lens)
        topology, trace = apply_trace(
            topology,
            asked.get("focus", "").strip(),
            direction=asked.get("direction", "both").strip(),
            depth=asked.get("depth", "2").strip(),
        )
        # The whole map leaves its quiet lanes and unlinked cards out until
        # asked. A view or a trace is already a question, and shows its answer.
        lanes = _Lanes.of(
            self.node_items(topology, lens_name, trace),
            everything=bool(active_lens or trace or asked.get("all")),
            focus=trace.focus if trace else "",
        )
        context.update(
            topology=topology,
            topology_groups=lanes.groups,
            # Passed rather than written into the template: the window is one
            # number, and a page that restates it drifts from the query that
            # produced the figures the moment either changes.
            traffic_window_days=HOST_TRAFFIC_DAYS,
            topology_detail=lanes.detail,
            topology_left_out=lanes.left_out,
            # "N of M shown": the cards drawn, out of every card, the left-out
            # ones too.
            topology_shown=lanes.shown,
            topology_total=lanes.left_out + lanes.shown,
            topology_everything=bool(asked.get("all")) and not (active_lens or trace),
            topology_lenses=topology_lenses(),
            active_lens=active_lens,
            trace_reset_url=_reset_url(lens_name),
            **self._trace_context(topology, trace, lens_name),
        )
        return context

    @classmethod
    def _trace_context(cls, topology, trace: TopologyTrace | None, lens_name: str) -> dict[str, Any]:
        """What the page says about a trace: its ledger, its focus, and the
        links that follow it another way or to another depth."""

        if trace is None:
            return {
                "topology_trace": None,
                "topology_edges": (),
                "focus_node": "",
                "topology_trace_focus": None,
                "topology_trace_label": "",
                "trace_direction_links": (),
                "trace_depth_links": (),
            }
        by_id = {node.id: node for node in topology.nodes}
        return {
            "topology_trace": trace,
            # The ledger restates every edge the node bodies already state
            # from both ends, so it is drawn for a bounded trace only.
            "topology_edges": tuple(
                {
                    "edge": edge,
                    "source": by_id[edge.source],
                    "target": by_id[edge.target],
                    "source_link": node_link(by_id[edge.source]),
                    "target_link": node_link(by_id[edge.target]),
                }
                for edge in topology.edges
            ),
            "focus_node": trace.focus,
            "topology_trace_focus": by_id.get(trace.focus),
            "topology_trace_label": dict(_TRACE_DIRECTIONS).get(trace.direction, ""),
            "trace_direction_links": tuple(
                {"name": name, "label": label, "url": cls._trace_url(trace.focus, name, lens_name, trace.depth)}
                for name, label in _TRACE_DIRECTIONS
            ),
            "trace_depth_links": tuple(
                {"depth": depth, "url": cls._trace_url(trace.focus, trace.direction, lens_name, depth)}
                for depth in range(1, 6)
            ),
        }

    @classmethod
    def node_items(
        cls,
        topology,
        lens_name: str = "",
        trace=None,
        *,
        only: str = "",
    ) -> list[dict[str, Any]]:
        """What the page states about each node, in projection order.

        ``only`` narrows to one node: its relations still come from every edge,
        so a node body fetched on its own says exactly what the page would.
        """

        by_id = {node.id: node for node in topology.nodes}
        hops = dict(trace.hops) if trace else {}
        neighbors: dict[str, set[str]] = {node.id: set() for node in topology.nodes}
        # An edge is a verb with a direction. Collapsing it to an undirected
        # neighbor set answers "is this related" and throws away "how" and
        # "which way", which is the only part an operator is actually reading.
        # Both ends get a row so a node can state its relationships from where
        # it stands, without the reader re-deriving the arrow.
        relations: dict[str, list[dict[str, Any]]] = {node.id: [] for node in topology.nodes}
        for edge in topology.edges:
            if edge.source not in neighbors or edge.target not in neighbors:
                continue
            neighbors[edge.source].add(edge.target)
            neighbors[edge.target].add(edge.source)
            evidence = {
                "detail": "" if edge.entities else edge.detail,
                "entities": edge.entities,
                "observed_age": cls._observed_age(edge.observed_at),
                "observed_at": edge.observed_at,
            }
            relation = RELATIONS.get(edge.kind)
            relations[edge.source].append(
                {
                    "direction": "out",
                    "rank": relation_rank(edge),
                    "label": edge.label,
                    "other": by_id[edge.target],
                    "other_link": node_link(by_id[edge.target]),
                    "status": edge.status,
                    "url": cls._focus_link(edge.target, lens_name),
                    **evidence,
                }
            )
            relations[edge.target].append(
                {
                    "direction": "in",
                    "rank": relation_rank(edge),
                    # Said from where this node stands: a machine "Serves" a
                    # service that "Runs on" it.
                    "label": relation.inverse if relation and relation.phrase else edge.label,
                    "other": by_id[edge.source],
                    "other_link": node_link(by_id[edge.source]),
                    "status": edge.status,
                    "url": cls._focus_link(edge.source, lens_name),
                    **evidence,
                }
            )
        items = []
        for node in topology.nodes:
            if only and node.id != only:
                continue
            rows = sorted(
                relations[node.id],
                key=lambda row: (
                    row["direction"],
                    row["rank"],
                    row["label"],
                    row["other"].label.casefold(),
                ),
            )
            items.append(
                {
                    "node": node,
                    "link": node_link(node),
                    "neighbors": " ".join(sorted(neighbors[node.id])),
                    "degree": len(neighbors[node.id]),
                    "relations": rows,
                    "observed_age": cls._observed_age(node.observed_at),
                    "observable": observable(node),
                    "hop": hops.get(node.id),
                    "focus_url": cls._focus_link(node.id, lens_name),
                    "body_url": (
                        f"{reverse('control_plane:topology_node')}?"
                        + urlencode({"node": node.id, **({"lens": lens_name} if lens_name else {})})
                    ),
                    "inbound_url": cls._trace_url(node.id, "inbound", lens_name),
                    "outbound_url": cls._trace_url(node.id, "outbound", lens_name),
                }
            )
        return items

    @staticmethod
    def _observed_age(observed_at: str) -> datetime | None:
        """The observation instant as a datetime, so a template can age it.

        A node carries the instant as ISO 8601 text because the projection is
        serialized to JSON as often as it is rendered, and `timesince` needs
        the object back. Unparseable text ages to nothing rather than raising:
        the reading is a fact about the world, not an invariant of ours.
        """

        return moment(observed_at, naive="keep")

    @staticmethod
    def _focus_link(node_id: str, lens: str = "") -> str:
        """Focus one node, keeping the active lens and letting depth default."""

        return topology_url(node_id, lens=lens)

    @staticmethod
    def _trace_url(focus: str, direction: str, lens: str = "", depth: int = 3) -> str:
        return topology_url(focus, direction=direction, depth=depth, lens=lens)


class TopologyNodeView(TemplateView):
    """One node's body, for the page to fetch when the node is opened.

    The page draws every node's summary and leaves the bodies to this, so
    its weight follows how many nodes there are rather than how much each
    one says.
    """

    template_name = "control_plane/_topology_node_body.html"

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        topology = derive_topology(principal=web_principal(self.request.user), request=self.request)
        active_lens = lens_for(self.request.GET.get("lens", "").strip())
        if active_lens is not None:
            topology = apply_lens(topology, active_lens)
        items = TopologyView.node_items(
            topology,
            active_lens.name if active_lens else "",
            only=self.request.GET.get("node", "").strip(),
        )
        if not items:
            raise Http404("No such node.")
        context.update(item=items[0], traffic_window_days=HOST_TRAFFIC_DAYS)
        return context
