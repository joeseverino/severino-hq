"""A machine read that names something HQ does not have is refused with the
choices that would narrow it, and a read with no narrowing answers what the
estate holds rather than all of it."""

from django.test import TestCase
from pydantic import ValidationError

from hq.domains.control_plane.models import ManagedResource

from ..findings import finding_rules, findings
from ..projection import QueryRefusal
from ..record_errors import RecordNotFound
from ..refusal_status import refusal_of
from ..security import Capability, Principal
from ..topology import derive_topology, topology
from ..topology_lenses import topology_lenses
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
        """What either door answers for ``narrowing``, which names get_topology."""

        with host_only(), self.assertRaises((QueryRefusal, ValidationError, RecordNotFound)) as refused:
            topology(principal=READ, **narrowing)
        return refusal_of(refused.exception, name="get_topology").message

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

    def test_an_unknown_focus_is_not_found_and_says_where_ids_come_from(self):
        with host_only(), self.assertRaises(RecordNotFound):
            topology(principal=READ, focus="resource:not-a-record")
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

    def test_a_known_focus_answers_its_neighborhood(self):
        with host_only():
            traced = topology(principal=READ, focus="resource:lonely-record", direction="inbound", depth=1)

        self.assertEqual(traced["trace"]["focus"], "resource:lonely-record")
        self.assertIn("resource:lonely-record", {node["id"] for node in traced["nodes"]})


class FindingsNarrowingTests(TestCase):
    def test_a_known_rule_narrows_to_it(self):
        rule = finding_rules()[0].name
        with host_only():
            narrowed = findings(principal=READ, rule=rule)

        self.assertEqual(narrowed["rule"], rule)
        self.assertTrue(all(item["rule"] == rule for item in narrowed["findings"]))
