from django.test import TestCase

from ..topology_lenses import apply_trace
from ..topology_model import Topology, TopologyEdge, TopologyNode


class TopologyTraceTests(TestCase):
    def setUp(self):
        self.graph = Topology(
            nodes=tuple(
                TopologyNode(node_id, "resource", node_id.upper(), "Example")
                for node_id in ("a", "b", "c", "d", "aside")
            ),
            edges=(
                TopologyEdge("ab", "a", "b", "feeds", "Feeds"),
                TopologyEdge("bc", "b", "c", "feeds", "Feeds"),
                TopologyEdge("cd", "c", "d", "feeds", "Feeds"),
                TopologyEdge("aside-a", "aside", "a", "feeds", "Feeds"),
            ),
        )

    def test_a_depth_that_is_not_a_number_follows_two_hops_and_one_below_one_follows_one(self):
        _, unreadable = apply_trace(self.graph, "a", direction="outbound", depth="many")
        _, too_shallow = apply_trace(self.graph, "a", direction="outbound", depth=0)

        self.assertEqual(unreadable.depth, 2)
        self.assertEqual(dict(unreadable.hops), {"a": 0, "b": 1, "c": 2})
        self.assertEqual(too_shallow.depth, 1)
        self.assertEqual(dict(too_shallow.hops), {"a": 0, "b": 1})
