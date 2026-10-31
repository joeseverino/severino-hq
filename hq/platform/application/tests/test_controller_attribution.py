"""Which controller a stale kind is laid at: only one the topology proves."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from ..findings import derive_findings
from ..security import Capability, Principal
from ..topology_model import Topology, TopologyEdge, TopologyNode

MANAGE = Principal("operator", "test", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


def _stale(node_id: str, kind: str) -> TopologyNode:
    old = (timezone.now() - timedelta(days=3)).isoformat()
    return TopologyNode(node_id, "resource", node_id, "Resource", kind_key=kind, observed_at=old)


def _edge(source: str, target: str, kind: str) -> TopologyEdge:
    return TopologyEdge(f"{kind}:{source}:{target}", source, target, kind, kind)


class ControllerAttributionTests(TestCase):
    def test_kinds_a_controller_s_connection_uses_are_one_claim_on_it(self):
        topology = Topology(
            nodes=(
                TopologyNode("controller:one", "controller", "One", "Controller"),
                TopologyNode("connection:one", "connection", "One", "Connection"),
                _stale("resource:a", "example.a"),
                _stale("resource:b", "example.b"),
            ),
            edges=(
                _edge("controller:one", "connection:one", "carries"),
                _edge("connection:one", "resource:a", "used_by"),
                _edge("connection:one", "resource:b", "used_by"),
            ),
        )

        (claim,) = derive_findings(topology, principal=MANAGE)

        self.assertEqual(claim.rule, "controller-sweep-stale")
        self.assertEqual(claim.subject, "controller:one")
        self.assertEqual(claim.affected_scopes, ("example.a", "example.b"))

    def test_a_connection_no_controller_carries_lays_nothing_at_one(self):
        topology = Topology(
            nodes=(
                TopologyNode("controller:one", "controller", "One", "Controller"),
                TopologyNode("connection:one", "connection", "One", "Connection"),
                TopologyNode("ability:a", "ability", "Read A", "Ability"),
                _stale("resource:a", "example.a"),
                _stale("resource:b", "example.b"),
            ),
            edges=(
                _edge("connection:one", "resource:a", "used_by"),
                _edge("connection:one", "ability:a", "enables"),
                _edge("ability:a", "resource:b", "governs"),
            ),
        )

        raised = derive_findings(topology, principal=MANAGE)

        self.assertEqual([item.rule for item in raised], ["kind-never-swept"] * 2)

    def test_what_names_no_node_or_no_kind_is_laid_at_no_controller(self):
        topology = Topology(
            nodes=(
                TopologyNode("controller:one", "controller", "One", "Controller"),
                TopologyNode("connection:one", "connection", "One", "Connection"),
                TopologyNode("ability:a", "ability", "Read A", "Ability"),
                TopologyNode("resource:plain", "resource", "Plain", "Resource"),
                _stale("resource:a", "example.a"),
            ),
            edges=(
                _edge("controller:one", "connection:one", "carries"),
                _edge("connection:one", "resource:a", "used_by"),
                _edge("connection:one", "resource:gone", "used_by"),
                _edge("connection:one", "resource:plain", "used_by"),
                _edge("connection:one", "ability:a", "enables"),
                _edge("ability:a", "resource:gone", "governs"),
                _edge("ability:a", "resource:plain", "governs"),
            ),
        )

        raised = derive_findings(topology, principal=MANAGE)

        # One kind alone is no shared failure, so the controller is not named.
        self.assertEqual([item.rule for item in raised], ["kind-never-swept"])
