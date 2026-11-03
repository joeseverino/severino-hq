"""A machine read that names something HQ does not have is refused with the
choices that would narrow it, and a read with no narrowing answers what the
estate holds rather than all of it."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource

from ..findings import finding_rules, findings
from ..projection import QueryRefusal
from ..security import AuthorizationError, Capability, Principal
from ..topology import derive_topology, topology
from ..topology_lenses import TRACE_DIRECTIONS, topology_lenses
from .host_only import host_only

READ = Principal("reader", "test", frozenset({Capability.READ}))
NONE = Principal("nobody", "test", frozenset())


class TopologyNarrowingTests(TestCase):
    def setUp(self):
        ManagedResource.objects.create(
            key="lonely-record",
            kind="adguard.rewrite",
            spec={"domain": "lonely.example.test", "answer": "192.0.2.10"},
        )

    def refusal(self, **narrowing) -> str:
        with host_only(), self.assertRaises(QueryRefusal) as refused:
            topology(principal=READ, **narrowing)
        return refused.exception.reason

    def test_no_narrowing_answers_the_counts_and_the_lenses_without_nodes(self):
        with host_only():
            answer = topology(principal=READ)
            lensed = topology(principal=READ, lens="unobserved-resources")

        self.assertNotIn("nodes", answer)
        self.assertNotIn("edges", answer)
        self.assertGreater(answer["summary"]["nodes"], 0)
        self.assertEqual(sum(answer["summary"]["kinds"].values()), answer["summary"]["nodes"])
        self.assertEqual([item["name"] for item in answer["lenses"]], [lens.name for lens in topology_lenses()])
        # A narrowed answer carries its nodes.
        self.assertIn("resource:lonely-record", {node["id"] for node in lensed["nodes"]})

    def test_an_unknown_lens_is_refused_with_every_lens_name(self):
        reason = self.refusal(lens="not-a-lens")

        for lens in topology_lenses():
            self.assertIn(lens.name, reason)
        self.assertNotIn("not-a-lens", reason)

    def test_an_unknown_focus_is_refused_and_says_where_ids_come_from(self):
        reason = self.refusal(focus="resource:not-a-record")

        self.assertIn("lens", reason)
        self.assertNotIn("not-a-record", reason)

    def test_a_focus_outside_the_lens_is_refused(self):
        with host_only():
            every = {node.id for node in derive_topology(principal=READ).nodes}
            kept = {node["id"] for node in topology(principal=READ, lens="unobserved-resources")["nodes"]}
        outside = sorted(every - kept)
        self.assertTrue(outside)

        self.refusal(lens="unobserved-resources", focus=outside[0])

    def test_an_unknown_direction_is_refused_with_every_direction(self):
        reason = self.refusal(focus="resource:lonely-record", direction="sideways")

        for direction in TRACE_DIRECTIONS:
            self.assertIn(direction, reason)

    def test_a_depth_that_is_not_a_hop_count_is_refused(self):
        for depth in (0, 99):
            with self.subTest(depth=depth):
                self.refusal(focus="resource:lonely-record", depth=depth)

    def test_a_caller_without_read_is_denied_before_any_input_is_judged(self):
        with host_only():
            with self.assertRaises(AuthorizationError):
                topology(principal=NONE, lens="not-a-lens")
            with self.assertRaises(AuthorizationError):
                findings(principal=NONE, rule="not-a-rule")

    def test_a_known_focus_answers_its_neighborhood(self):
        with host_only():
            traced = topology(principal=READ, focus="resource:lonely-record", direction="inbound", depth=1)

        self.assertEqual(traced["trace"]["focus"], "resource:lonely-record")
        self.assertIn("resource:lonely-record", {node["id"] for node in traced["nodes"]})


class FindingsNarrowingTests(TestCase):
    def test_an_unknown_rule_is_refused_with_every_rule_name(self):
        with host_only(), self.assertRaises(QueryRefusal) as refused:
            findings(principal=READ, rule="not-a-rule")

        for rule in finding_rules():
            self.assertIn(rule.name, refused.exception.reason)
        self.assertNotIn("not-a-rule", refused.exception.reason)

    def test_a_known_rule_narrows_to_it(self):
        rule = finding_rules()[0].name
        with host_only():
            narrowed = findings(principal=READ, rule=rule)

        self.assertEqual(narrowed["rule"], rule)
        self.assertTrue(all(item["rule"] == rule for item in narrowed["findings"]))
