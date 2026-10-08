"""The topology workspace and the drill-in for one node."""

from datetime import datetime
from typing import Any, override
from urllib.parse import urlencode

from django.http import Http404
from django.views.generic import TemplateView

from hq.platform.application.action_links import topology_url
from hq.platform.application.analytics import HOST_TRAFFIC_DAYS
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
from hq.platform.application.topology_model import RELATIONS, observable, relation_rank
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


class TopologyView(PageMixin, TemplateView):
    """The live, actionable graph derived by the application layer."""

    template_name = "control_plane/topology.html"
    page_title = "Map"

    @override
    def get_page_actions(self):
        return (
            PageAction("Add a record", reverse("control_plane:create"), primary=True),
            PageAction("All records", reverse("control_plane:list")),
            PageAction("Connections", reverse("control_plane:connections")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        topology = derive_topology(
            principal=web_principal(self.request.user), request=self.request
        )
        active_lens = lens_for(self.request.GET.get("lens", "").strip())
        if active_lens is not None:
            topology = apply_lens(topology, active_lens)
        requested_focus = self.request.GET.get("focus", "").strip()
        topology, trace = apply_trace(
            topology,
            requested_focus,
            direction=self.request.GET.get("direction", "both").strip(),
            depth=self.request.GET.get("depth", "2").strip(),
        )
        by_id = {node.id: node for node in topology.nodes}
        lens_name = active_lens.name if active_lens else ""
        groups: dict[str, list[dict[str, Any]]] = {}
        detail = None
        # The whole map leaves its quiet lanes and unlinked cards out until
        # asked. A view or a trace is already a question, and shows its answer.
        everything = bool(active_lens or trace or self.request.GET.get("all"))
        left_out = 0
        for item in self.node_items(topology, lens_name, trace):
            if not everything and not kept_on_the_map(item["node"].kind, item["degree"]):
                left_out += 1
                continue
            groups.setdefault(item["node"].kind, []).append(item)
            if trace and item["node"].id == trace.focus:
                detail = item
        # One heading, noun and plural per lane: the heading names the lane,
        # and a count says one or the other.
        nouns = {kind: _lane_nouns(kind) for kind in groups}
        trace_directions = _TRACE_DIRECTIONS
        context.update(
            {
                "topology": topology,
                "topology_groups": tuple(
                    {
                        "kind": kind,
                        "label": nouns[kind][0],
                        "count": counted(len(items), *nouns[kind][1:]),
                        "items": items,
                    }
                    for kind, items in groups.items()
                ),
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
                )
                if trace
                else (),
                # Passed rather than written into the template: the window is
                # one number, and a page that restates it drifts from the query
                # that produced the figures the moment either changes.
                "traffic_window_days": HOST_TRAFFIC_DAYS,
                "focus_node": trace.focus if trace else "",
                # The focused node's body, drawn once in the panel below the
                # lanes rather than inside its card.
                "topology_detail": detail,
                "topology_left_out": left_out,
                "topology_everything": bool(self.request.GET.get("all")) and not (active_lens or trace),
                "topology_lenses": topology_lenses(),
                "active_lens": active_lens,
                "topology_trace": trace,
                "topology_trace_focus": by_id.get(trace.focus) if trace else None,
                "topology_trace_label": (
                    dict(_TRACE_DIRECTIONS).get(trace.direction, "") if trace else ""
                ),
                "trace_direction_links": tuple(
                    {
                        "name": name,
                        "label": label,
                        "url": self._trace_url(
                            trace.focus,
                            name,
                            active_lens.name if active_lens else "",
                            trace.depth,
                        ),
                    }
                    for name, label in trace_directions
                )
                if trace
                else (),
                "trace_depth_links": tuple(
                    {
                        "depth": depth,
                        "url": self._trace_url(
                            trace.focus,
                            trace.direction,
                            active_lens.name if active_lens else "",
                            depth,
                        ),
                    }
                    for depth in range(1, 6)
                )
                if trace
                else (),
                "trace_reset_url": (
                    f"{reverse('control_plane:topology')}?"
                    f"{urlencode({'lens': active_lens.name})}#map"
                    if active_lens
                    else f"{reverse('control_plane:topology')}#map"
                ),
            }
        )
        return context

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
        # neighbour set answers "is this related" and throws away "how" and
        # "which way", which is the only part an operator is actually reading.
        # Both ends get a row so a node can state its relationships from where
        # it stands, without the reader re-deriving the arrow.
        relations: dict[str, list[dict[str, Any]]] = {
            node.id: [] for node in topology.nodes
        }
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
                        + urlencode(
                            {"node": node.id, **({"lens": lens_name} if lens_name else {})}
                        )
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
        topology = derive_topology(
            principal=web_principal(self.request.user), request=self.request
        )
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
