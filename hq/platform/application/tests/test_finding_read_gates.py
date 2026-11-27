"""Every finding carries the read gate of what it is about, so the one reader
filter (``attention.readable_by``) holds it back from a reader who may not
read that thing, including a finding about a whole kind or a check of HQ's
standard, which has no subject node."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.platform.core.testing import operator

from .. import roles
from ..connections import ConnectionInstance, ConnectionLink, ConnectionSpec
from ..derivation_testing import uncached
from ..inventory_testing import store
from ..plugin_testing import offering
from ..projection import projection_scope
from ..security import Capability, cli_principal, web_principal
from ..topology import derive_topology


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
        credential_model="none",
    )
    return ConnectionSpec(f"example.{name}", name.title(), "Example.", capability, mock.Mock(return_value=(instance,)))


class WhatAConnectionNamesTests(TestCase):
    """A controller or target exists in the topology only because a family's instance names it."""

    def gates(self, *specs) -> dict[str, set[str]]:
        with offering(*specs), uncached(), projection_scope():
            topology = derive_topology(principal=cli_principal())
        return {node.label: set(node.read_capabilities) for node in topology.nodes if node.kind != "connection"}

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
        user = operator()
        roles.assign(user, roles.Role.VIEWER)
        self.viewer = web_principal(get_user_model().objects.get(pk=user.pk))

    def machine(self, *specs):
        """The machine node as the shared derivation holds it, and every relation of it the viewer may read."""

        with offering(*specs), uncached(), projection_scope():
            topology = derive_topology(principal=cli_principal())
        by_id = {node.id: node for node in topology.nodes}
        (machine,) = [node for node in topology.nodes if node.kind == "machine" and node.label == "example-host"]
        relations = sorted(
            (edge.kind, edge.source, edge.target)
            for edge in topology.edges
            if machine.id in (edge.source, edge.target)
            and self.viewer.permits(*by_id[edge.target if edge.source == machine.id else edge.source].read_capabilities)
        )
        return machine, relations

    def test_a_read_only_reader_of_the_machine_is_told_nothing_of_the_gated_family(self):
        gated = naming("infra", Capability.MANAGE_INFRASTRUCTURE, "example-host", controller="example-host")
        alone, alone_relations = self.machine()
        named, named_relations = self.machine(gated)

        self.assertTrue(self.viewer.permits(*named.read_capabilities))
        for part, before, after in (
            ("facts", alone.facts, named.facts),
            ("actions", alone.actions, named.actions),
            ("relations", alone_relations, named_relations),
        ):
            with self.subTest(part):
                self.assertEqual(after, before)
