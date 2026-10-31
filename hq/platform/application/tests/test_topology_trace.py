from django.test import TestCase

from ..topology import serialize_topology
from ..topology_lenses import MAX_TRACE_DEPTH, apply_trace
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

    def test_outbound_trace_is_bounded_and_records_shortest_hops(self):
        narrowed, trace = apply_trace(self.graph, "a", direction="outbound", depth=2)

        self.assertEqual({node.id for node in narrowed.nodes}, {"a", "b", "c"})
        self.assertEqual(dict(trace.hops), {"a": 0, "b": 1, "c": 2})
        self.assertNotIn("d", {node.id for node in narrowed.nodes})

    def test_inbound_and_outbound_are_distinct_questions(self):
        inbound, _ = apply_trace(self.graph, "a", direction="inbound", depth=3)
        outbound, _ = apply_trace(self.graph, "a", direction="outbound", depth=3)

        self.assertEqual({node.id for node in inbound.nodes}, {"a", "aside"})
        self.assertEqual({node.id for node in outbound.nodes}, {"a", "b", "c", "d"})

    def test_invalid_inputs_are_safe_and_depth_is_capped(self):
        unchanged, trace = apply_trace(self.graph, "missing", depth="many")
        capped, capped_trace = apply_trace(self.graph, "a", direction="sideways", depth=999)

        self.assertIs(unchanged, self.graph)
        self.assertIsNone(trace)
        self.assertEqual(capped_trace.direction, "both")
        self.assertEqual(capped_trace.depth, MAX_TRACE_DEPTH)
        self.assertLessEqual(len(capped.nodes), len(self.graph.nodes))

    def test_a_depth_that_is_not_a_number_follows_two_hops_and_one_below_one_follows_one(self):
        _, unreadable = apply_trace(self.graph, "a", direction="outbound", depth="many")
        _, too_shallow = apply_trace(self.graph, "a", direction="outbound", depth=0)

        self.assertEqual(unreadable.depth, 2)
        self.assertEqual(dict(unreadable.hops), {"a": 0, "b": 1, "c": 2})
        self.assertEqual(too_shallow.depth, 1)
        self.assertEqual(dict(too_shallow.hops), {"a": 0, "b": 1})

    def test_both_directions_follow_each_edge_either_way_and_keep_only_edges_inside(self):
        narrowed, trace = apply_trace(self.graph, "b", direction="both", depth=1)

        self.assertEqual(dict(trace.hops), {"b": 0, "a": 1, "c": 1})
        self.assertEqual({edge.id for edge in narrowed.edges}, {"ab", "bc"})

    def test_a_trace_past_the_graph_s_end_stops_at_what_it_reached(self):
        _, trace = apply_trace(self.graph, "c", direction="outbound", depth=MAX_TRACE_DEPTH)

        self.assertEqual(dict(trace.hops), {"c": 0, "d": 1})

    def test_serialization_carries_the_trace_without_mutating_nodes(self):
        narrowed, trace = apply_trace(self.graph, "a", direction="outbound", depth=2)
        payload = serialize_topology(narrowed, trace=trace)

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["trace"]["focus"], "a")
        self.assertEqual(
            payload["trace"]["hops"],
            [
                {"node": "a", "hop": 0},
                {"node": "b", "hop": 1},
                {"node": "c", "hop": 2},
            ],
        )
        self.assertNotIn("hop", payload["nodes"][0])
