"""The topology HQ derives: its nodes, what carries what, and what each node measures."""

from django.test import TestCase

from hq.platform.core.testing import READER, connection, declare

from ..connections import ConnectionInstance, ConnectionLink, ConnectionSpec
from ..plugin_testing import offering
from ..security import Capability
from ..topology import derive_topology
from .sentinel import SentinelMixin


class DerivedTopologyTests(SentinelMixin, TestCase):
    def setUp(self):
        self.resource = declare(
            "example-zone", "cloudflare.zone", {"zone": "example.com", "connection_ref": "example-cloudflare"}
        )
        connection(
            "cloudflare_dns",
            "example-cloudflare",
            endpoint="https://api.example.test/client/v4",
            reaches=["example.com"],
        )

    def test_dependency_label_cannot_impersonate_a_resource_key(self):
        spec = ConnectionSpec(
            "example.collision",
            "Collision check",
            "A synthetic connection with an ambiguous rendered label.",
            Capability.READ,
            lambda: (
                ConnectionInstance(
                    "one",
                    "One",
                    "example",
                    "good",
                    "Healthy",
                    dependencies=(ConnectionLink("example-zone", "https://example.test/elsewhere"),),
                    credential_model="none",
                ),
            ),
        )
        with offering(spec, alone=True):
            topology = derive_topology(principal=READER)

        self.assertNotIn(
            (
                "connection:example.collision:one",
                "resource:example-zone",
                "used_by",
            ),
            {(edge.source, edge.target, edge.kind) for edge in topology.edges},
        )
