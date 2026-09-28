"""The topology workspace and the drill-in for one node."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from contextlib import suppress

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.urls import reverse
from django.views.generic import TemplateView

from application.entity_links import NODE_KINDS, node_link
from application.analytics import HOST_TRAFFIC_DAYS
from application.action_links import (
    topology_url,
)
from application.topology import derive_topology
from application.topology_lenses import (
    apply_lens,
    apply_trace,
    lens_for,
    topology_lenses,
)
from application.topology_model import (
    RELATIONS,
    relation_rank,
    observable,
)
from application.security import web_principal
from application.pages import PageAction, PageMixin
from application.ui import counted


class TopologyView(PageMixin, LoginRequiredMixin, TemplateView):
    """The live, actionable graph derived by the application layer."""

    template_name = "control_plane/topology.html"
    page_title = "Topology"
    page_lede = (
        "Relationships between declared resources, observed systems and connections."
    )

    def get_page_actions(self):
        return (
            PageAction("Add resource", reverse("control_plane:create"), primary=True),
            PageAction("Resources", reverse("control_plane:list")),
            PageAction("Connections", reverse("control_plane:connections")),
        )

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
        for item in self.node_items(topology, lens_name, trace):
            groups.setdefault(item["node"].kind, []).append(item)
            if trace and item["node"].id == trace.focus:
                detail = item
        # One noun and its plural per kind, from the node kind registry: the
        # heading is the plural, and a count says one or the other.
        nouns = {kind: (item.noun, item.plural) for kind, item in NODE_KINDS.items()}
        trace_directions = (
            ("inbound", "Incoming"),
            ("outbound", "Outgoing"),
            ("both", "Both directions"),
        )
        context.update(
            {
                "topology": topology,
                "topology_groups": tuple(
                    {
                        "kind": kind,
                        "label": nouns[kind][1].capitalize(),
                        "count": counted(len(items), *nouns[kind]),
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
                "topology_lenses": topology_lenses(),
                "active_lens": active_lens,
                "topology_trace": trace,
                "topology_trace_focus": by_id.get(trace.focus) if trace else None,
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

        if not observed_at:
            return None
        with suppress(ValueError):
            return datetime.fromisoformat(observed_at)
        return None

    @staticmethod
    def _focus_link(node_id: str, lens: str = "") -> str:
        """Focus one node, keeping the active lens and letting depth default."""

        return topology_url(node_id, lens=lens)

    @staticmethod
    def _trace_url(focus: str, direction: str, lens: str = "", depth: int = 3) -> str:
        return topology_url(focus, direction=direction, depth=depth, lens=lens)


class TopologyNodeView(LoginRequiredMixin, TemplateView):
    """One node's body, for the page to fetch when the node is opened.

    The page draws every node's summary and leaves the bodies to this, so
    its weight follows how many nodes there are rather than how much each
    one says.
    """

    template_name = "control_plane/_topology_node_body.html"

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
