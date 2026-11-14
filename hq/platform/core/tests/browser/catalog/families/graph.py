"""The catalog's graphs: the map of a topology, its lanes of cards and the
panel of the selected one, and the edges a node stands at the end of, drawn
from an example graph through the view that draws the map's page."""

from datetime import timedelta

from hq.domains.control_plane.topology_views import TopologyView
from hq.platform.application.entity_links import EntityLink
from hq.platform.application.topology_lenses import apply_trace
from hq.platform.application.topology_model import Topology, TopologyEdge, TopologyNode
from hq.platform.core.tests.browser.catalog.common import DRAWN_AT, Entry

_READ = (DRAWN_AT - timedelta(minutes=40)).isoformat()

# Every lane the map draws, a card in each standing and one whose name is
# long enough to wrap, a lane of context nothing links to, and edges of
# every shape a node's panel states: a verb alone, one with its reading's
# detail and age, and one carrying the records its reading names.
_NODES = (
    TopologyNode("controller:example", "controller", "example-controller", "Controller", status="good"),
    TopologyNode(
        "machine:example-host-01",
        "machine",
        "example-host-01",
        "Machine",
        status="good",
        status_label="Online",
        detail="An example line on why it stands as it does.",
        url="/example/machines/example-host-01/",
        observed_at=_READ,
    ),
    TopologyNode(
        "machine:example-host-02",
        "machine",
        "example-host-02",
        "Machine",
        status="attention",
        status_label="Out of date",
        url="/example/machines/example-host-02/",
    ),
    TopologyNode(
        "service:app.example.com",
        "service",
        "app.example.com",
        "Service",
        status="serious",
        url="/example/services/app.example.com/",
        pageviews=1200,
        visits=310,
    ),
    TopologyNode(
        "service:example-service-with-a-long-hostname.example.com",
        "service",
        "example-service-with-a-long-hostname.example.com",
        "Service",
        url="/example/services/long/",
    ),
    TopologyNode("zone:example.com", "zone", "example.com", "Domain", url="/example/domains/example.com/"),
    TopologyNode(
        "resource:example-record",
        "resource",
        "example-record",
        "Example record",
        summary="192.0.2.10",
        url="/example/records/example-record/",
        managed=False,
        declared_revision=2,
        unconfirmed_fields=("answer", "ttl"),
    ),
    TopologyNode("dependency:example-upstream", "dependency", "example-upstream", "Not in HQ"),
)
_EDGES = (
    TopologyEdge("e1", "service:app.example.com", "machine:example-host-01", "runs_on", "Runs on", status="good"),
    TopologyEdge(
        "e2",
        "service:example-service-with-a-long-hostname.example.com",
        "machine:example-host-01",
        "runs_on",
        "Runs on",
    ),
    TopologyEdge("e3", "zone:example.com", "service:app.example.com", "contains", "Names"),
    TopologyEdge(
        "e4",
        "machine:example-host-02",
        "machine:example-host-01",
        "talks_to",
        "Talks to",
        status="attention",
        detail="Through an example tunnel",
        observed_at=_READ,
    ),
    TopologyEdge(
        "e5",
        "machine:example-host-01",
        "resource:example-record",
        "reading",
        "Answers for",
        observed_at=_READ,
        entities=(
            EntityLink("192.0.2.10", "/example/addresses/192.0.2.10/", kind_label="Address"),
            EntityLink("app.example.com", "/example/services/app.example.com/", kind_label="Service"),
        ),
    ),
    TopologyEdge("e6", "controller:example", "machine:example-host-01", "reaches", "Reads"),
)
_GRAPH = Topology(nodes=_NODES, edges=_EDGES)


def _map(focus="", **asked):
    """The map's page context for the example graph, traced from ``focus``."""

    topology, trace = apply_trace(_GRAPH, focus)
    return TopologyView.drawn(topology, trace=trace, **asked)


_MAP = '{% include "partials/_topology_map.html" %}'
# A map beside another on the gallery's page, its ids its own.
_ANOTHER_MAP = '{% include "partials/_topology_map.html" with map_id=map_id %}'

ENTRIES = {
    # The whole map, its quiet lane and unlinked card left out until asked,
    # with nothing selected.
    "graph/map": Entry(_MAP, _map()),
    # Everything asked for, and one node selected: its details beside the
    # lanes where there is room and under them where there is not.
    "graph/map-selected": Entry(_ANOTHER_MAP, {**_map("machine:example-host-01"), "map_id": "example-map-selected"}),
    # A map with nothing on it.
    "graph/map-empty": Entry(
        _ANOTHER_MAP, {**TopologyView.drawn(Topology(nodes=(), edges=())), "map_id": "example-map-empty"}
    ),
    # The edges a node stands at the end of, both ways, in a section of its own.
    "graph/relations": Entry(
        '{% section "Example host" %}{% include "partials/_relation_list.html" with relations=item.relations only %}'
        "{% endsection %}",
        {"item": _map("machine:example-host-01")["topology_detail"]},
    ),
}
