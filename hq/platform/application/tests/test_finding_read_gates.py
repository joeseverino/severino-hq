"""Every finding carries the read gate of what it is about, so the one reader
filter (``attention.readable_by``) holds it back from a reader who may not
read that thing, including a finding about a whole kind or a check of HQ's
standard, which has no subject node."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS

from .. import findings, roles
from ..attention import finding_insight, readable_by
from ..connection_setup import setups
from ..connections import ConnectionInstance, ConnectionLink, ConnectionSpec
from ..dashboard import queue_item
from ..derivations import uncached
from ..finding_model import Finding
from ..inventory_testing import store
from ..plugin_testing import offering
from ..posture import STANDARD
from ..projection import projection_scope
from ..security import Capability, Principal, cli_principal, web_principal
from ..topology import derive_topology
from ..topology_model import RELATIONS, Topology, TopologyNode

READ = Principal("reader", "test", frozenset({Capability.READ}))
MANAGE = Principal("operator", "test", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


def gated_family(credential_model: str):
    """A family only infrastructure management may read, whose one instance
    holds a token of ``credential_model`` for a service that offers scoped ones."""

    scoped = next(setup.provider for setup in setups() if setup.permissions)
    instance = ConnectionInstance(
        "example-infra",
        "Example infrastructure",
        scoped,
        "good",
        "Good",
        credential_model=credential_model,
    )
    return ConnectionSpec(
        "example.infrastructure",
        "Infrastructure authority",
        "Requires infrastructure management.",
        Capability.MANAGE_INFRASTRUCTURE,
        mock.Mock(return_value=(instance,)),
    )


def unknown(**fields) -> Finding:
    return Finding(rule="example", title="Example", severity="attention", explanation="Example.", **fields)


@override_settings(SEVERINO_POSTURE="advanced")
class TheSeededEstateTests(TestCase):
    """An estate that raises findings about nodes, about a whole kind and about a check of HQ's standard."""

    def setUp(self):
        store("tailscale.device", {"name": "example-host", "online": True})
        # A declared kind nothing has read, and one read long ago.
        ManagedResource.objects.create(
            key="example-rewrite", kind="adguard.rewrite", spec={"domain": "a.example.test", "answer": "192.0.2.10"}
        )
        user = get_user_model().objects.create_user("example-viewer")
        roles.assign(user, roles.Role.VIEWER)
        self.viewer = web_principal(get_user_model().objects.get(pk=user.pk))

    def raised(self, credential_model: str = "coarse"):
        operator = cli_principal()
        with offering(gated_family(credential_model)), uncached(), projection_scope():
            topology = derive_topology(principal=operator)
            return topology, findings.derive_findings(topology, principal=operator)

    def told(self, principal, credential_model: str = "coarse") -> list[dict]:
        """Every finding the operator's estate raises, as a queue item, after the one reader filter."""

        _topology, raised = self.raised(credential_model)
        url = reverse("control_plane:findings")
        items = [queue_item("hq.findings", "Findings", finding_insight(finding, url)) for finding in raised]
        return readable_by(principal, items)

    def test_every_finding_has_a_gate_derived_from_what_it_is_about(self):
        topology, raised = self.raised()
        by_id = {node.id: node for node in topology.nodes}

        self.assertTrue(any(not finding.subject for finding in raised), "the estate raised no scope-level finding")
        for finding in raised:
            with self.subTest(finding.rule, subject=finding.subject, scope=finding.scope):
                gate = findings.read_gate(finding, by_id.get(finding.subject))
                self.assertIsNotNone(gate, f"rule {finding.rule!r} raised a finding whose read gate is underivable")
                self.assertEqual(finding.read_capabilities, gate)

    def test_a_check_that_names_a_gated_instance_takes_its_family_s_gate(self):
        _topology, raised = self.raised()

        (scope,) = [finding for finding in raised if finding.scope == "posture.scope"]

        self.assertIn(Capability.MANAGE_INFRASTRUCTURE, scope.read_capabilities)

    def test_a_read_only_reader_is_told_nothing_gated_above_read(self):
        told = self.told(self.viewer)

        self.assertTrue(told, "the viewer was told nothing, so the filter was not exercised")
        for item in told:
            with self.subTest(item["label"]):
                self.assertLessEqual(set(item["read_capabilities"]), {Capability.READ})

    def test_a_check_about_a_gated_instance_reaches_only_a_reader_its_family_permits(self):
        for name, principal in (("viewer", self.viewer), ("operator", cli_principal())):
            with self.subTest(name):
                troubled, healthy = self.told(principal, "coarse"), self.told(principal, "scoped")
                if principal is self.viewer:
                    self.assertEqual(troubled, healthy)
                else:
                    self.assertNotEqual(troubled, healthy)


class EveryScopeTests(TestCase):
    """Every scope a finding can name reads through a resource, so its gate is that resource's."""

    def test_every_kind_and_check_derives_a_gate(self):
        scopes = (*PROVIDERS, *OBSERVATIONS, *(f"posture.{check.id}" for check in STANDARD))
        for scope in scopes:
            with self.subTest(scope):
                self.assertTrue(findings.read_gate(unknown(subject="", scope=scope), None))

    def test_a_node_s_gate_is_its_own_beside_the_read_of_any_node(self):
        node = TopologyNode("connection:example", "connection", "Example", "Connection", read_capabilities=("example",))

        gate = findings.read_gate(unknown(subject=node.id), node)

        self.assertEqual(set(gate), {Capability.READ, "example"})

    def test_a_higher_order_finding_takes_every_scope_s_gate(self):
        kind = next(iter(PROVIDERS))
        node = TopologyNode("controller:one", "controller", "One", "Controller", read_capabilities=("example",))

        gate = findings.read_gate(unknown(subject=node.id, affected_scopes=(kind, "example.unread")), node)

        self.assertIsNone(gate)
        self.assertTrue(findings.read_gate(unknown(subject=node.id, affected_scopes=(kind,)), node))

    def test_what_names_no_node_and_no_known_scope_is_underivable(self):
        for name, finding, node in (
            ("no subject or scope", unknown(subject=""), None),
            ("a scope nothing reads", unknown(subject="", scope="example.unread"), None),
            ("a subject the topology lacks", unknown(subject="resource:gone", scope=next(iter(PROVIDERS))), None),
        ):
            with self.subTest(name):
                self.assertIsNone(findings.read_gate(finding, node))

    def test_an_underivable_gate_is_held_at_the_authority_that_derived_it(self):
        old = (timezone.now() - timedelta(days=3)).isoformat()
        topology = Topology(
            nodes=(TopologyNode("resource:a", "resource", "A", "Resource", kind_key="example.a", observed_at=old),),
            edges=(),
        )

        (claim,) = findings.derive_findings(topology, principal=MANAGE)

        self.assertEqual(claim.rule, "kind-never-swept")
        self.assertTrue(MANAGE.permits(*claim.read_capabilities))
        self.assertFalse(READ.permits(*claim.read_capabilities))


def naming(name: str, capability: Capability, *names: str, controller: str = "example-controller") -> ConnectionSpec:
    """A family read under ``capability`` whose instance runs on ``controller`` and reaches each of ``names``."""

    instance = ConnectionInstance(
        f"{name}-one",
        f"Example {name}",
        "example",
        "good",
        "Good",
        controller_id=controller,
        targets=tuple(ConnectionLink(label) for label in names),
    )
    return ConnectionSpec(f"example.{name}", name.title(), "Example.", capability, mock.Mock(return_value=(instance,)))


class WhatAConnectionNamesTests(TestCase):
    """A controller or target exists in the topology only because a family's instance names it."""

    def gates(self, *specs) -> dict[str, set[str]]:
        with offering(*specs), uncached(), projection_scope():
            topology = derive_topology(principal=cli_principal())
        return {node.label: set(node.read_capabilities) for node in topology.nodes if node.kind != "connection"}

    def test_what_only_a_gated_family_names_takes_its_gate(self):
        gates = self.gates(naming("infra", Capability.MANAGE_INFRASTRUCTURE, "example-target"))

        for label in ("example-controller", "example-target"):
            with self.subTest(label):
                self.assertIn(Capability.MANAGE_INFRASTRUCTURE, gates[label])

    def test_what_a_family_any_reader_may_read_also_names_takes_the_narrower_gate(self):
        gates = self.gates(
            naming("infra", Capability.MANAGE_INFRASTRUCTURE, "example-target"),
            naming("open", Capability.READ, "example-target"),
        )

        for label in ("example-controller", "example-target"):
            with self.subTest(label):
                self.assertNotIn(Capability.MANAGE_INFRASTRUCTURE, gates[label])


class AMachineAGatedFamilyNamesTests(TestCase):
    """A controller or target named as a machine is folded onto it only where the machine's readers may read it."""

    def setUp(self):
        store("tailscale.device", {"name": "example-host", "online": True})
        user = get_user_model().objects.create_user("example-viewer")
        roles.assign(user, roles.Role.VIEWER)
        self.viewer = web_principal(get_user_model().objects.get(pk=user.pk))

    def machine(self, *specs):
        """The machine node as the shared derivation holds it, and every relation of it the viewer may read."""

        with offering(*specs), uncached(), projection_scope():
            topology = derive_topology(principal=cli_principal())
        self.edges = {(edge.kind, edge.source, edge.target) for edge in topology.edges}
        by_id = {node.id: node for node in topology.nodes}
        (machine,) = [node for node in topology.nodes if node.kind == "machine" and node.label == "example-host"]
        relations = sorted(
            (edge.kind, edge.source, edge.target)
            for edge in topology.edges
            if machine.id in (edge.source, edge.target)
            and self.viewer.permits(*by_id[edge.target if edge.source == machine.id else edge.source].read_capabilities)
        )
        return machine, relations, by_id

    def test_a_read_only_reader_of_the_machine_is_told_nothing_of_the_gated_family(self):
        gated = naming("infra", Capability.MANAGE_INFRASTRUCTURE, "example-host", controller="example-host")
        alone, alone_relations, _ = self.machine()
        named, named_relations, _ = self.machine(gated)

        self.assertTrue(self.viewer.permits(*named.read_capabilities))
        for part, before, after in (
            ("facts", alone.facts, named.facts),
            ("actions", alone.actions, named.actions),
            ("relations", alone_relations, named_relations),
        ):
            with self.subTest(part):
                self.assertEqual(after, before)

    def test_what_the_gated_family_names_stays_its_own_node_under_its_gate(self):
        gated = naming("infra", Capability.MANAGE_INFRASTRUCTURE, "example-host", controller="example-host")
        machine, _relations, by_id = self.machine(gated)

        kept = [
            node for node in by_id.values() if node.kind in ("controller", "target") and node.label == "example-host"
        ]
        self.assertEqual({node.kind for node in kept}, {"controller", "target"})
        for node in kept:
            with self.subTest(node.kind):
                self.assertIn(Capability.MANAGE_INFRASTRUCTURE, node.read_capabilities)
                joined = {"controller": "runs_on", "target": "stands_for"}[node.kind]
                self.assertIn((joined, node.id, machine.id), self.edges)

    def test_what_a_family_any_reader_may_read_names_is_still_folded_onto_the_machine(self):
        machine, _relations, by_id = self.machine(
            naming("open", Capability.READ, "example-host", controller="example-host")
        )

        self.assertFalse(
            [node for node in by_id.values() if node.kind in ("controller", "target") and node.label == "example-host"]
        )
        # What a target kept apart is joined by, said from the machine's side.
        self.assertIn((RELATIONS["stands_for"].inverse, "example-host"), machine.facts)
