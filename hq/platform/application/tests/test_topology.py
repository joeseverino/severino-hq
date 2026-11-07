"""The topology HQ derives: its nodes, what carries what, and what each node measures."""

from datetime import timedelta
from unittest import mock

from django.db import connection as database_connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderConnection
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

from ..connections import ConnectionInstance, ConnectionLink, ConnectionSpec
from ..plugin_testing import offering
from ..security import Capability
from ..topology import TOPOLOGY_SCHEMA_VERSION, derive_topology, serialize_topology, topology as serialized_topology
from .host_only import host_only
from .sentinel import RESOURCE as SENTINEL_RESOURCE, SentinelMixin, plant_credentials
from .topology_principals import MANAGE, NONE, READ


class DerivedTopologyTests(SentinelMixin, TestCase):
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
        with host_only():
            return derive_topology(principal=principal)

    def test_declarations_observations_and_registries_form_one_graph(self):
        topology = self.project()
        nodes = {node.id: node for node in topology.nodes}
        edges = {(edge.source, edge.target, edge.kind) for edge in topology.edges}
        connection_id = "connection:infrastructure.controllers:example-controller:example-cloudflare"
        ability_id = "ability:infrastructure.controllers:cloudflare.zone"

        self.assertIn(connection_id, nodes)
        self.assertIn(ability_id, nodes)
        self.assertIn("resource:example-zone", nodes)
        controller = next(node for node in topology.nodes if node.kind == "controller")
        target_id = next(target for source, target, kind in edges if source == connection_id and kind == "reaches")
        target = nodes[target_id]
        self.assertEqual(target.label, "example.com")
        self.assertIn((controller.id, connection_id, "carries"), edges)
        self.assertIn((connection_id, ability_id, "enables"), edges)
        self.assertIn(
            (ability_id, "resource:example-zone", "governs"),
            edges,
        )
        self.assertIn((connection_id, "resource:example-zone", "used_by"), edges)
        self.assertIn((connection_id, target.id, "reaches"), edges)

    def test_a_controller_retains_every_distinct_connection_workflow(self):
        second = ConnectionSpec(
            "example.second",
            "Second connections",
            "Another family carried by the same controller.",
            Capability.READ,
            lambda: (
                ConnectionInstance(
                    "second",
                    "Second",
                    "example",
                    "good",
                    "Healthy",
                    controller_id="example-controller",
                ),
            ),
            web_route="control_plane:list",
        )

        with offering(second, alone=True):
            topology = derive_topology(principal=READ)

        controller = next(node for node in topology.nodes if node.kind == "controller")
        self.assertEqual(
            {action.url for action in controller.actions},
            {reverse("control_plane:connections"), reverse("control_plane:list")},
        )

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
                    dependencies=(ConnectionLink("example-zone", "https://example.test/elsewhere"),),
                ),
            ),
        )
        with offering(spec, alone=True):
            topology = derive_topology(principal=READ)

        self.assertNotIn(
            (
                "connection:example.collision:one",
                "resource:example-zone",
                "used_by",
            ),
            {(edge.source, edge.target, edge.kind) for edge in topology.edges},
        )

    # With public DNS changes off a domain is not offered for edit; that is
    # what the panel shares with its page, held in test_topology_offers.
    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_actions_are_existing_use_cases_and_follow_authorization(self):
        reader = next(node for node in self.project().nodes if node.id == "resource:example-zone")
        operator = next(node for node in self.project(MANAGE).nodes if node.id == "resource:example-zone")

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
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        resource = ManagedResource.objects.create(
            key="internal-name",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

        node = next(node for node in self.project(MANAGE).nodes if node.id == f"resource:{resource.key}")

        reconcile = next(action for action in node.actions if action.name == "reconcile")
        self.assertEqual(reconcile.method, "POST")
        self.assertEqual(reconcile.capability, "infrastructure.resource.reconcile")

    def test_disabled_resource_is_named_honestly_and_cannot_reconcile(self):
        resource = ManagedResource.objects.create(
            key="paused-record",
            kind="adguard.rewrite",
            spec={"domain": "paused.example.test", "answer": "192.0.2.11"},
            enabled=False,
        )

        node = next(node for node in self.project(MANAGE).nodes if node.id == f"resource:{resource.key}")

        self.assertEqual(node.status, "neutral")
        self.assertEqual(
            [action.name for action in node.actions],
            ["open", "edit", "remove"],
        )

    def test_read_is_required_before_any_provider_is_invoked(self):
        with (
            mock.patch("hq.platform.application.topology.connection_catalog") as catalog,
            self.assertRaisesRegex(PermissionError, "lacks 'read'"),
        ):
            derive_topology(principal=NONE)

        catalog.assert_not_called()

    def test_serialization_is_normalized_and_carries_no_credential_a_controller_reported(self):
        plant_credentials()

        payload = serialize_topology(self.project(MANAGE))

        self.assertEqual(payload["schema_version"], TOPOLOGY_SCHEMA_VERSION)
        self.assertEqual(payload["summary"]["nodes"], len(payload["nodes"]))
        self.assertEqual(payload["summary"]["edges"], len(payload["edges"]))
        self.assertIn(f"resource:{SENTINEL_RESOURCE}", {node["id"] for node in payload["nodes"]})
        self.assertNoSentinel(payload)

    def test_query_count_does_not_grow_with_resource_count(self):
        def arrange(count):
            for index in range(ManagedResource.objects.filter(key__startswith="example-zone-").count(), count):
                ManagedResource.objects.create(
                    key=f"example-zone-{index}",
                    kind="cloudflare.zone",
                    spec={"zone": f"{index}.example.com", "connection_ref": "example-cloudflare"},
                )

        def zones(topology):
            return sum(node.id.startswith("resource:example-zone-") for node in topology.nodes)

        for principal in (READ, MANAGE):
            with self.subTest(principal=principal.actor), host_only():
                ManagedResource.objects.filter(key__startswith="example-zone-").delete()
                assert_cost_does_not_grow(
                    self,
                    sizes=(1, 20),
                    arrange=arrange,
                    draw=lambda _count, principal=principal: derive_topology(principal=principal),
                    rows=zones,
                )


class MeasuredNodeTests(TestCase):
    """A node named like a host carries what that host served, by name alone."""

    def _measure(self, host, *, pageviews=412, visits=300):
        from hq.domains.analytics.models import AnalyticsSite, RumDaily

        site = AnalyticsSite.objects.create(site_tag=f"tag-{host}", host=host)
        RumDaily.objects.create(
            site=site,
            date=timezone.now().date() - timedelta(days=1),
            dimension=RumDaily.Dimension.PATH,
            value="/",
            pageviews=pageviews,
            visits=visits,
            sample_interval=1,
        )

    def _spec(self, *hosts):
        return ConnectionSpec(
            "example.measured",
            "Measured routes",
            "A synthetic connection family that reaches named hosts.",
            Capability.READ,
            lambda: (
                ConnectionInstance(
                    "one",
                    "One",
                    "example",
                    "good",
                    "Healthy",
                    targets=tuple(ConnectionLink(host) for host in hosts),
                ),
            ),
        )

    def _nodes(self, *hosts):
        with offering(self._spec(*hosts), alone=True):
            return {n.label: n for n in derive_topology(principal=READ).nodes}

    def test_a_target_named_like_a_measured_host_carries_its_traffic_and_lends_it_to_no_other(self):
        self._measure("measured.example.com", pageviews=412, visits=300)

        nodes = self._nodes("measured.example.com", "other.example.com")

        measured = nodes["measured.example.com"]
        self.assertEqual((measured.pageviews, measured.visits), (412, 300))
        self.assertIsNone(nodes["other.example.com"].pageviews)

    def test_an_unmeasured_host_carries_none_rather_than_zero(self):
        # The distinction the field exists for: nobody visited and nobody looked
        # are opposite findings, and only the second is worth acting on.
        node = self._nodes("unmeasured.example.com")["unmeasured.example.com"]

        self.assertIsNone(node.pageviews)
        self.assertIsNone(node.visits)

    def test_a_graph_with_no_hostname_shaped_node_costs_no_extra_query(self):
        # Why this can live in the shared projection rather than in one adapter:
        # a deployment measuring nothing pays nothing for the seam.
        with offering(self._spec("not-a-hostname"), alone=True):
            derive_topology(principal=READ)
            with CaptureQueriesContext(database_connection) as queries:
                derive_topology(principal=READ)

        self.assertEqual([q for q in queries.captured_queries if "analytics_rumdaily" in q["sql"]], [])

    def test_the_measurement_reaches_every_adapter_not_just_the_page(self):
        self._measure("measured.example.com")

        with offering(self._spec("measured.example.com"), alone=True):
            measured = next(n for n in derive_topology(principal=READ).nodes if n.label == "measured.example.com")
            payload = serialized_topology(principal=READ, focus=measured.id, depth=1)

        node = next(n for n in payload["nodes"] if n["label"] == "measured.example.com")
        self.assertEqual(node["pageviews"], 412)


class OneNodePerMachineTests(TestCase):
    """A machine is one node, whatever else calls it, and its declaration hangs off it."""

    def setUp(self):
        from hq.domains.control_plane.models import ProviderInventory

        ManagedResource.objects.create(
            key="a-docker-host",
            kind="machine",
            spec={"name": "a-docker-host", "addresses": ["10.0.0.9", "100.64.0.9"]},
        )
        ManagedResource.objects.create(
            key="a-service",
            kind="portainer.container",
            spec={"connection_ref": "a-portainer", "host": "a-docker-host", "name": "a-service"},
        )
        ManagedResource.objects.create(
            key="a-docker-host-tailnet",
            kind="tailscale.device",
            spec={"name": "a-docker-host"},
        )
        ProviderInventory.objects.create(
            kind="tailscale.device",
            observed_at=timezone.now(),
            records=[{"name": "a-docker-host", "addresses": ["100.64.0.9"]}],
        )

    def _project(self, *targets):
        spec = ConnectionSpec(
            "example.carried",
            "Carried connections",
            "A synthetic family carried by a declared machine.",
            Capability.READ,
            lambda: (
                ConnectionInstance(
                    "one",
                    "One",
                    "example",
                    "good",
                    "Healthy",
                    controller_id="a-docker-host",
                    targets=tuple(ConnectionLink(target) for target in targets),
                ),
            ),
        )
        with offering(spec, alone=True):
            topology = derive_topology(principal=READ)
        return (
            {node.id: node for node in topology.nodes},
            {(edge.source, edge.target, edge.kind) for edge in topology.edges},
        )

    def test_the_machine_is_one_node_and_what_runs_on_it_hangs_off_it(self):
        nodes, edges = self._project("10.0.0.9")

        # The controller it runs is the machine, not a second node.
        self.assertEqual([node for node in nodes.values() if node.kind == "controller"], [])
        machine = nodes["machine:a-docker-host"]
        self.assertEqual(machine.kind, "machine")
        self.assertIn(("Runs the controller", "a-docker-host"), machine.facts)
        self.assertTrue(any(source == "machine:a-docker-host" and kind == "carries" for source, _, kind in edges))
        self.assertIn(("machine:a-docker-host", "resource:a-docker-host", "declared_by"), edges)
        self.assertIn(("machine:a-docker-host", "resource:a-service", "runs"), edges)
        # Its tailnet device is joined by the address they share.
        self.assertIn(("machine:a-docker-host", "resource:a-docker-host-tailnet", "on_tailnet"), edges)
        # No edge is left pointing at a folded node.
        for source, target, _ in edges:
            self.assertIn(source, nodes)
            self.assertIn(target, nodes)

    def test_a_target_at_its_address_is_the_machine(self):
        nodes, edges = self._project("10.0.0.9")

        self.assertNotIn("10.0.0.9", {node.label for node in nodes.values()})
        self.assertIn(("Reached as", "10.0.0.9"), nodes["machine:a-docker-host"].facts)
        self.assertTrue(any(target == "machine:a-docker-host" and kind == "reaches" for _, target, kind in edges))

    def test_a_target_no_machine_answers_for_stays_its_own_node(self):
        nodes, _ = self._project("elsewhere.example.com")

        self.assertIn("elsewhere.example.com", {node.label for node in nodes.values()})


class AnUnrecognizedContainerIsCarriedByItsMachineTests(TestCase):
    """A container the sweep declined has no node, so its machine holds it."""

    def setUp(self):
        from ..security import cli_principal
        from ..sweep import record_sweep

        ManagedResource.objects.create(
            key="a-docker-host",
            kind="machine",
            spec={"name": "a-docker-host", "addresses": ["10.0.0.9"]},
        )
        record_sweep(
            {
                "portainer.container": {
                    "ok": True,
                    "records": [
                        {
                            "name": "a-web",
                            "host": "a-docker-host",
                            "connection_ref": "a-portainer",
                            "stack": "a-project",
                        },
                        {"name": "a-stray", "host": "a-docker-host", "connection_ref": "a-portainer", "stack": ""},
                    ],
                }
            },
            principal=cli_principal(),
        )
        with host_only():
            self.nodes = {node.id: node for node in derive_topology(principal=READ).nodes}

    def test_it_is_a_fact_on_its_machine_node_and_a_declared_one_is_not(self):
        facts = self.nodes["machine:a-docker-host"].facts

        self.assertEqual(
            [value for key, value in facts if key == "unrecognized-container"],
            ["a-stray@a-docker-host"],
        )

    def test_it_has_no_node_of_its_own(self):
        self.assertNotIn("a-stray", {node.label for node in self.nodes.values()})


class HolderEdgeTests(TestCase):
    """Every kind mirroring a provider's records is joined to the connection
    holding its live record, by declaring ``from_record`` and nothing else."""

    CONNECTION = "connection:infrastructure.controllers:example-controller:example-holder"

    def _edges(self, kind, *, declared, record):
        from hq.domains.control_plane.models import ProviderInventory

        ManagedResource.objects.all().delete()
        ProviderInventory.objects.all().delete()
        ProviderConnection.objects.all().delete()
        provider = PROVIDERS[kind]
        ManagedResource.objects.create(key="example-declared", kind=kind, spec=declared)
        ProviderInventory.objects.create(kind=kind, observed_at=timezone.now(), records=[record])
        ProviderConnection.objects.create(
            controller_id="example-controller",
            connection_ref="example-holder",
            provider=(provider.connection_providers or ("example",))[0],
            endpoint="https://api.example.test/",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )
        with host_only():
            topology = derive_topology(principal=READ)
        return {(edge.source, edge.target, edge.kind) for edge in topology.edges}

    def mirrored(self):
        return [
            (kind, provider)
            for kind, provider in PROVIDERS.items()
            if provider.from_record is not None and provider.sample_record
        ]

    def test_every_mirrored_kind_is_used_by_the_connection_holding_its_record(self):
        self.assertTrue(self.mirrored())
        for kind, provider in self.mirrored():
            with self.subTest(kind=kind):
                record = {**provider.sample_record, "connection_ref": "example-holder"}
                edges = self._edges(kind, declared=provider.from_record(record), record=record)

                self.assertIn((self.CONNECTION, "resource:example-declared", "used_by"), edges)

    def test_a_record_naming_no_connection_is_held_by_its_providers_connections(self):
        kind, provider = next((kind, provider) for kind, provider in self.mirrored() if provider.connection_providers)
        record = dict(provider.sample_record)
        edges = self._edges(kind, declared=provider.from_record(record), record=record)

        self.assertIn((self.CONNECTION, "resource:example-declared", "used_by"), edges)

    def test_holding_a_record_keeps_the_status_of_an_edge_already_drawn(self):
        kind, provider = self.mirrored()[0]
        record = {**provider.sample_record, "connection_ref": "example-holder"}
        # The declaration also names the connection it uses, and that connection is failing.
        declared = {**provider.from_record(record), "connection_ref": "example-holder"}
        self._edges(kind, declared=declared, record=record)
        ProviderConnection.objects.update(reachable=False)
        with host_only():
            topology = derive_topology(principal=READ)

        statuses = {
            edge.status
            for edge in topology.edges
            if (edge.source, edge.target, edge.kind) == (self.CONNECTION, "resource:example-declared", "used_by")
        }
        self.assertEqual(statuses, {"serious"})

    def test_a_record_naming_another_connection_draws_no_edge_from_this_one(self):
        kind, provider = self.mirrored()[0]
        record = {**provider.sample_record, "connection_ref": "example-elsewhere"}
        edges = self._edges(kind, declared=provider.from_record(record), record=record)

        self.assertNotIn((self.CONNECTION, "resource:example-declared", "used_by"), edges)
