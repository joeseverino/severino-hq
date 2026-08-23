from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection as database_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderConnection

from .connections import ConnectionInstance, ConnectionLink, ConnectionSpec
from .security import Capability, Principal
from .topology import derive_topology, serialize_topology


READ = Principal("reader", "test", frozenset({Capability.READ}))
MANAGE = Principal(
    "operator",
    "test",
    frozenset(
        {
            Capability.READ,
            Capability.MANAGE_INFRASTRUCTURE,
            Capability.REQUEST_CERTIFICATE_RENEWAL,
        }
    ),
)
NONE = Principal("nobody", "test", frozenset())


class DerivedTopologyTests(TestCase):
    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="example-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "example-cloudflare"},
        )
        ProviderConnection.objects.create(
            controller_id="example-controller",
            connection_ref="example-cloudflare",
            provider="cloudflare_dns",
            endpoint="https://api.example.test/client/v4",
            reaches=["example.com"],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

    def project(self, principal=READ):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            return derive_topology(principal=principal)

    def test_declarations_observations_and_registries_form_one_graph(self):
        topology = self.project()
        nodes = {node.id: node for node in topology.nodes}
        edges = {(edge.source, edge.target, edge.kind) for edge in topology.edges}
        connection_id = (
            "connection:infrastructure.controllers:"
            "example-controller:example-cloudflare"
        )
        ability_id = "ability:infrastructure.controllers:cloudflare.zone"

        self.assertIn(connection_id, nodes)
        self.assertIn(ability_id, nodes)
        self.assertIn("resource:example-zone", nodes)
        controller = next(
            node for node in topology.nodes if node.kind == "controller"
        )
        target = next(node for node in topology.nodes if node.kind == "target")
        self.assertEqual(target.label, "example.com")
        self.assertIn((controller.id, connection_id, "carries"), edges)
        self.assertIn(
            (connection_id, ability_id, "enables"), edges
        )
        self.assertIn(
            (ability_id, "resource:example-zone", "governs"),
            edges,
        )
        self.assertIn((connection_id, "resource:example-zone", "used_by"), edges)
        self.assertIn((connection_id, target.id, "reaches"), edges)

    def test_declared_ability_remains_visible_without_a_live_connection(self):
        resource = ManagedResource.objects.create(
            key="example-tailnet-device",
            kind="tailscale.device",
            spec={"hostname": "example-device"},
        )

        topology = self.project()
        nodes = {node.id for node in topology.nodes}
        edges = {(edge.source, edge.target, edge.kind) for edge in topology.edges}

        ability_id = "ability:infrastructure.controllers:tailscale.device"
        self.assertIn(ability_id, nodes)
        self.assertIn(
            (
                ability_id,
                f"resource:{resource.key}",
                "governs",
            ),
            edges,
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
                    dependencies=(
                        ConnectionLink("example-zone", "https://example.test/elsewhere"),
                    ),
                ),
            ),
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            topology = derive_topology(principal=READ)

        self.assertNotIn(
            (
                "connection:example.collision:one",
                "resource:example-zone",
                "used_by",
            ),
            {(edge.source, edge.target, edge.kind) for edge in topology.edges},
        )

    def test_actions_are_existing_use_cases_and_follow_authorization(self):
        reader = next(
            node for node in self.project().nodes if node.id == "resource:example-zone"
        )
        operator = next(
            node
            for node in self.project(MANAGE).nodes
            if node.id == "resource:example-zone"
        )

        self.assertEqual([action.name for action in reader.actions], ["open"])
        self.assertEqual(
            [action.name for action in operator.actions],
            ["open", "edit", "remove"],
        )
        remove = operator.actions[-1]
        self.assertEqual(remove.capability, "infrastructure.resource.remove")
        self.assertEqual(remove.target, "example-zone")
        self.assertEqual(remove.method, "GET")

    def test_unrelated_controller_tools_do_not_gate_resource_manipulation(self):
        resource = ManagedResource.objects.create(
            key="internal-name",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

        node = next(
            node
            for node in self.project(MANAGE).nodes
            if node.id == f"resource:{resource.key}"
        )

        reconcile = next(action for action in node.actions if action.name == "reconcile")
        self.assertEqual(reconcile.method, "POST")
        self.assertEqual(reconcile.capability, "infrastructure.reconcile")

    def test_disabled_resource_is_named_honestly_and_cannot_reconcile(self):
        resource = ManagedResource.objects.create(
            key="paused-record",
            kind="adguard.rewrite",
            spec={"domain": "paused.example.test", "answer": "192.0.2.11"},
            enabled=False,
        )

        node = next(
            node
            for node in self.project(MANAGE).nodes
            if node.id == f"resource:{resource.key}"
        )

        self.assertEqual(node.status, "neutral")
        self.assertEqual(node.status_label, "Disabled")
        self.assertEqual(
            [action.name for action in node.actions],
            ["open", "edit", "remove"],
        )

    def test_read_is_required_before_any_provider_is_invoked(self):
        with (
            mock.patch("application.topology.connection_catalog") as catalog,
            self.assertRaisesRegex(PermissionError, "lacks 'read'"),
        ):
            derive_topology(principal=NONE)

        catalog.assert_not_called()

    def test_serialization_is_normalized_and_contains_no_credential_material(self):
        payload = serialize_topology(self.project(MANAGE))
        serialized = json.dumps(payload)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["summary"]["nodes"], len(payload["nodes"]))
        self.assertEqual(payload["summary"]["edges"], len(payload["edges"]))
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("access_token", serialized.lower())

    def test_query_count_does_not_grow_with_resource_count(self):
        principals = (READ, MANAGE)
        small_counts = {}
        large_counts = {}
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            for principal in principals:
                with CaptureQueriesContext(database_connection) as small:
                    derive_topology(principal=principal)
                small_counts[principal.actor] = len(small)
            for index in range(20):
                ManagedResource.objects.create(
                    key=f"example-zone-{index}",
                    kind="cloudflare.zone",
                    spec={
                        "zone": f"{index}.example.com",
                        "connection_ref": "example-cloudflare",
                    },
                )
            for principal in principals:
                with CaptureQueriesContext(database_connection) as large:
                    derive_topology(principal=principal)
                large_counts[principal.actor] = len(large)

        self.assertEqual(large_counts, small_counts)


class TopologyPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)
        ManagedResource.objects.create(
            key="internal-name",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

    def test_page_is_progressively_enhanced_and_actions_remain_real_forms(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            response = self.client.get(reverse("control_plane:topology"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-topology")
        self.assertContains(response, "Relationship ledger")
        self.assertContains(response, 'class="visually-hidden">Filter topology')
        self.assertContains(response, '<fieldset class="topology-kind-filters">')
        self.assertContains(response, '<div class="table-scroll">')
        self.assertContains(response, 'id="map" tabindex="-1"')
        self.assertNotContains(response, "table-scroll table-sticky-header")
        self.assertContains(response, "internal-name")
        self.assertContains(
            response,
            f'action="{reverse("control_plane:reconcile", kwargs={"key": "internal-name"})}"',
        )
        self.assertNotContains(response, "<script>")

    def test_focus_accepts_only_a_node_that_exists(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            response = self.client.get(
                reverse("control_plane:topology"), {"focus": "not-a-node"}
            )

        self.assertEqual(response.context["focus_node"], "")
