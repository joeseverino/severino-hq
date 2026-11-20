"""The topology map's example: a graph of every lane the map draws."""

from datetime import timedelta

from hq.platform.core.part_examples import DRAWN_AT
from hq.platform.core.template_parts import Example
lazy from hq.domains.control_plane.topology_views import TopologyView
lazy from hq.platform.application.entity_links import EntityLink
lazy from hq.platform.application.topology_lenses import apply_trace
lazy from hq.platform.application.topology_model import Topology, TopologyEdge, TopologyNode


def _graph():
    # Every lane the map draws, a card in each standing and one whose name is
    # long enough to wrap, a lane of context nothing links to, and edges of
    # every shape a node's panel states: a verb alone, one with its reading's
    # detail and age, and one carrying the records its reading names.
    read = (DRAWN_AT - timedelta(minutes=40)).isoformat()
    nodes = (
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
            observed_at=read,
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
    edges = (
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
            observed_at=read,
        ),
        TopologyEdge(
            "e5",
            "machine:example-host-01",
            "resource:example-record",
            "reading",
            "Answers for",
            observed_at=read,
            entities=(
                EntityLink("192.0.2.10", "/example/addresses/192.0.2.10/", kind_label="Address"),
                EntityLink("app.example.com", "/example/services/app.example.com/", kind_label="Service"),
            ),
        ),
        TopologyEdge("e6", "controller:example", "machine:example-host-01", "reaches", "Reads"),
    )
    return Topology(nodes=nodes, edges=edges)


def topology_map(focus="", **asked):
    """The map's page context for the example graph, traced from ``focus``."""

    topology, trace = apply_trace(_graph(), focus)
    return TopologyView.drawn(topology, trace=trace, **asked)


# The whole map, its quiet lane and unlinked card left out until asked, with
# nothing selected.
TOPOLOGY_MAP = Example(context=topology_map)
