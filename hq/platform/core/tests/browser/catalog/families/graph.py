"""The catalog's graphs: the map of a topology, its lanes of cards and the
panel of the selected one, and the edges a node stands at the end of, drawn
from an example graph through the view that draws the map's page."""

from hq.domains.control_plane.topology_views import TopologyView
from hq.platform.application.topology_model import Topology
from hq.platform.core.part_examples.graph import topology_map
from hq.platform.core.template_parts import Example

# A map beside another on the gallery's page, its ids its own.
_ANOTHER_MAP = '{% include "partials/_topology_map.html" with map_id=map_id %}'

ENTRIES = {
    # Everything asked for, and one node selected: its details beside the
    # lanes where there is room and under them where there is not.
    "graph/map-selected": Example(
        _ANOTHER_MAP, {**topology_map("machine:example-host-01"), "map_id": "example-map-selected"}
    ),
    # A map with nothing on it.
    "graph/map-empty": Example(
        _ANOTHER_MAP, {**TopologyView.drawn(Topology(nodes=(), edges=())), "map_id": "example-map-empty"}
    ),
    # The edges a node stands at the end of, both ways, in a section of its own.
    "graph/relations": Example(
        '{% section "Example host" %}{% include "partials/_relation_list.html" with relations=item.relations only %}'
        "{% endsection %}",
        {"item": topology_map("machine:example-host-01")["topology_detail"]},
    ),
}
