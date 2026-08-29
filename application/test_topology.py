from __future__ import annotations

from datetime import timedelta
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection as database_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderConnection
from control_plane.views import TopologyView

from .command_center import command_center
from .connections import (
    ConnectionAbility,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from .security import Capability, Principal
from .topology import (
    MAX_TRACE_DEPTH,
    Topology,
    TopologyEdge,
    TopologyNode,
    apply_lens,
    apply_trace,
    derive_topology,
    lens_for,
    serialize_topology,
    topology as serialized_topology,
    topology_lenses,
)


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

        self.assertEqual(payload["schema_version"], 2)
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
        narrowed, trace = apply_trace(
            self.graph, "a", direction="outbound", depth=2
        )

        self.assertEqual({node.id for node in narrowed.nodes}, {"a", "b", "c"})
        self.assertEqual(dict(trace.hops), {"a": 0, "b": 1, "c": 2})
        self.assertNotIn("d", {node.id for node in narrowed.nodes})

    def test_inbound_and_outbound_are_distinct_questions(self):
        inbound, _ = apply_trace(self.graph, "a", direction="inbound", depth=3)
        outbound, _ = apply_trace(self.graph, "a", direction="outbound", depth=3)

        self.assertEqual({node.id for node in inbound.nodes}, {"a", "aside"})
        self.assertEqual(
            {node.id for node in outbound.nodes}, {"a", "b", "c", "d"}
        )

    def test_invalid_inputs_are_safe_and_depth_is_capped(self):
        unchanged, trace = apply_trace(self.graph, "missing", depth="many")
        capped, capped_trace = apply_trace(
            self.graph, "a", direction="sideways", depth=999
        )

        self.assertIs(unchanged, self.graph)
        self.assertIsNone(trace)
        self.assertEqual(capped_trace.direction, "both")
        self.assertEqual(capped_trace.depth, MAX_TRACE_DEPTH)
        self.assertLessEqual(len(capped.nodes), len(self.graph.nodes))

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

    def test_focus_becomes_a_shareable_bounded_trace(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            response = self.client.get(
                reverse("control_plane:topology"),
                {
                    "focus": "resource:internal-name",
                    "direction": "inbound",
                    "depth": "1",
                },
            )

        trace = response.context["topology_trace"]
        self.assertEqual(trace.focus, "resource:internal-name")
        self.assertEqual(trace.direction, "inbound")
        self.assertEqual(trace.depth, 1)
        self.assertContains(response, "Bounded trace")
        self.assertContains(response, "Trace outgoing")
        self.assertContains(response, "Clear trace")

    def page(self):
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            return self.client.get(reverse("control_plane:topology"))

    def test_a_node_title_links_to_the_thing_it_names(self):
        """A node carries a URL; a title that renders as text throws it away."""

        response = self.page()

        detail = reverse("control_plane:detail", kwargs={"key": "internal-name"})
        self.assertContains(
            response,
            f'<a class="topology-node-link" href="{detail}">internal-name</a>',
        )
        # The anchor sits inside the title's <strong>, which both the stylesheet
        # and the explorer's status line read as the node's name.
        self.assertContains(response, '<strong><a class="topology-node-link"')
        # A multi-line {# … #} is not a comment in Django, it is text. One
        # holding the word <strong> renders an element, and the explorer reads
        # the first <strong> in a node as its title -- so the leak is silent
        # until the status line starts quoting the commentary.
        self.assertNotContains(response, "{#")
        self.assertNotContains(response, "#}")

    def test_a_node_states_each_edge_from_where_it_stands(self):
        """Direction is the half of an edge a neighbour list throws away."""

        response = self.page()

        ability_id = "ability:infrastructure.controllers:adguard.rewrite"
        relations = {
            item["node"].id: item["relations"]
            for group in response.context["topology_groups"]
            for item in group["items"]
        }
        self.assertEqual(
            [(row["direction"], row["label"], row["other"].id)
             for row in relations["resource:internal-name"]],
            [("in", "Governs", ability_id)],
        )
        self.assertEqual(
            [(row["direction"], row["label"], row["other"].id)
             for row in relations[ability_id]],
            [("out", "Governs", "resource:internal-name")],
        )

        # The same edge renders once as incoming and once as outgoing, and each
        # row walks to the other end.
        self.assertContains(
            response, '<span class="eyebrow topology-relation-heading">Incoming</span>'
        )
        self.assertContains(
            response, '<span class="eyebrow topology-relation-heading">Outgoing</span>'
        )
        self.assertContains(response, '<span class="topology-relation-verb">Governs</span>')
        self.assertContains(
            response,
            f'href="{TopologyView._focus_link("resource:internal-name")}"',
        )
        self.assertContains(response, f'href="{TopologyView._focus_link(ability_id)}"')
        self.assertContains(
            response, '<span class="topology-relation-other">internal-name</span>'
        )
        self.assertContains(response, '<span class="topology-relation-kind">resource</span>')

    def test_a_node_body_carries_the_triage_the_projection_already_derived(self):
        """Declared versus observed is the whole of triage; both were discarded."""

        ManagedResource.objects.create(
            key="behind-name",
            kind="adguard.rewrite",
            spec={"domain": "behind.example.test", "answer": "192.0.2.11"},
            status={"domain": "behind.example.test"},
            last_observed_at=timezone.now() - timedelta(hours=3),
            generation=4,
            observed_generation=2,
        )
        ManagedResource.objects.create(
            key="disabled-name",
            kind="adguard.rewrite",
            spec={"domain": "off.example.test", "answer": "192.0.2.12"},
            enabled=False,
        )

        response = self.page()

        # A comparison, not two raw numbers.
        self.assertContains(response, "Behind")
        self.assertContains(response, "Declared 4, last confirmed 2")
        # The field the reading declined to echo back, as the list it is.
        self.assertContains(response, "<li><code>answer</code></li>")
        self.assertContains(response, "Unconfirmed by the last reading")
        # Age, not an ISO timestamp -- and the absence of one said out loud.
        self.assertContains(response, "3\xa0hours ago")
        self.assertContains(response, "Never — nothing observes this")
        # A disabled declaration is not a finding.
        self.assertContains(response, ">Unmanaged</span>")

    def test_a_node_nothing_reaches_says_so_rather_than_drawing_an_empty_box(self):
        response = self.page()

        self.assertContains(response, "No derived relationships")


class ConnectionActionTests(TestCase):
    """A connection offers what its own spec declared, resolved generically."""

    def spec(self, **overrides) -> ConnectionSpec:
        return ConnectionSpec(
            "example.declared", "Declared routes",
            "A synthetic connection family that declares where it can be reached.",
            Capability.READ,
            lambda: (ConnectionInstance("one", "One", "example", "good", "Healthy"),),
            **overrides,
        )

    def project(self, spec, principal=READ):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=(spec,)):
            return derive_topology(principal=principal)

    def node(self, spec):
        return next(n for n in self.project(spec).nodes
                    if n.id == "connection:example.declared:one")

    def test_every_declared_route_becomes_its_own_action(self):
        node = self.node(self.spec(
            management_route="control_plane:list", setup_route="control_plane:create",
            documentation_url="https://docs.example.test/connections",
        ))
        self.assertEqual(
            [(a.name, a.url) for a in node.actions],
            [("open", reverse("control_plane:connections")),
             ("manage", reverse("control_plane:list")),
             ("set_up", reverse("control_plane:create")),
             ("documentation", "https://docs.example.test/connections")],
        )

    def test_the_same_route_twice_is_offered_once(self):
        node = self.node(self.spec(management_route="control_plane:connections"))
        self.assertEqual([a.name for a in node.actions], ["open"])

    def test_a_spec_web_route_replaces_the_default_destination(self):
        node = self.node(self.spec(web_route="control_plane:list"))
        self.assertEqual(node.url, reverse("control_plane:list"))

    def test_an_ability_naming_a_capability_reports_the_canonical_contract(self):
        spec = self.spec(abilities=(ConnectionAbility(
            "example.rotate", "Rotate", "Rotate the credential this connection carries.",
            effect="infrastructure_change", capability="infrastructure.reconcile"),))
        ability = next(n for n in self.project(spec, MANAGE).nodes
                       if n.id == "ability:example.declared:example.rotate")
        command = next(a for a in ability.actions if a.name == "command")
        self.assertEqual(command.capability, "infrastructure.reconcile")
        self.assertEqual(command.effect, "infrastructure_change")

    def test_an_ability_never_advertises_a_command_the_principal_cannot_run(self):
        spec = self.spec(abilities=(ConnectionAbility(
            "example.rotate", "Rotate", "Rotate the credential this connection carries.",
            effect="infrastructure_change", capability="infrastructure.reconcile"),))
        ability = next(n for n in self.project(spec, READ).nodes
                       if n.id == "ability:example.declared:example.rotate")

        self.assertEqual([a.name for a in ability.actions], ["focus"])

    def test_an_ability_without_a_capability_only_relates(self):
        spec = self.spec(abilities=(ConnectionAbility(
            "example.inspect", "Inspect", "Read what this connection sees."),))
        ability = next(n for n in self.project(spec).nodes
                       if n.id == "ability:example.declared:example.inspect")
        self.assertEqual([a.name for a in ability.actions], ["focus"])


class TopologyLensTests(TestCase):
    """A lens narrows the one projection; it never derives a second one."""

    def setUp(self):
        ManagedResource.objects.create(
            key="observed-zone", kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "example-cloudflare"})
        self.unobserved = ManagedResource.objects.create(
            key="lonely-record", kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"})
        ProviderConnection.objects.create(
            controller_id="example-controller", connection_ref="example-cloudflare",
            provider="cloudflare_dns", endpoint="https://api.example.test/client/v4",
            reaches=["example.com"], reachable=True, probed=True,
            observed_at=timezone.now())

    def project(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            return derive_topology(principal=READ)

    def narrowed(self, name):
        lens = lens_for(name)
        self.assertIsNotNone(lens, f"{name} is not a declared lens")
        return apply_lens(self.project(), lens)

    def test_every_declared_lens_names_a_distinct_question(self):
        names = [lens.name for lens in topology_lenses()]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(item.label and item.summary for item in topology_lenses()))

    def test_a_lens_selects_resources_no_connection_reports(self):
        selected = {n.id for n in self.narrowed("unobserved-resources").nodes}
        self.assertIn(f"resource:{self.unobserved.key}", selected)
        self.assertNotIn("resource:observed-zone", selected)

    def test_a_lens_selects_resources_no_ability_governs(self):
        ManagedResource.objects.create(key="unclaimed-kind", kind="example.unclaimed", spec={})
        selected = {n.id for n in self.narrowed("ungoverned-resources").nodes}
        self.assertIn("resource:unclaimed-kind", selected)
        self.assertNotIn("resource:observed-zone", selected)

    def test_a_lens_finds_what_a_sweep_skipped_while_confirming_siblings(self):
        """Health describes the last observation's content, never its age."""
        now = timezone.now()
        fresh = ManagedResource.objects.create(
            key="swept-record", kind="adguard.rewrite",
            spec={"domain": "fresh.example.test", "answer": "192.0.2.20"})
        skipped = ManagedResource.objects.create(
            key="skipped-record", kind="adguard.rewrite",
            spec={"domain": "skipped.example.test", "answer": "192.0.2.21"})
        ManagedResource.objects.filter(pk=fresh.pk).update(last_observed_at=now)
        ManagedResource.objects.filter(pk=skipped.pk).update(
            last_observed_at=now - timedelta(days=4))
        selected = {n.id for n in self.narrowed("stale-observations").nodes}
        self.assertIn("resource:skipped-record", selected)
        self.assertNotIn("resource:swept-record", selected)

    def test_a_narrowed_projection_never_keeps_a_half_present_edge(self):
        for lens in topology_lenses():
            with self.subTest(lens=lens.name):
                narrowed = apply_lens(self.project(), lens)
                ids = {n.id for n in narrowed.nodes}
                for edge in narrowed.edges:
                    self.assertIn(edge.source, ids)
                    self.assertIn(edge.target, ids)

    def test_a_lens_can_only_narrow_what_the_principal_already_saw(self):
        whole = self.project()
        whole_ids = {n.id for n in whole.nodes}
        for lens in topology_lenses():
            with self.subTest(lens=lens.name):
                narrowed = apply_lens(whole, lens)
                self.assertLessEqual(len(narrowed.nodes), len(whole.nodes))
                self.assertLessEqual({n.id for n in narrowed.nodes}, whole_ids)

    def test_serialization_names_the_applied_lens_and_every_available_one(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            applied = serialized_topology(principal=READ, lens="unobserved-resources")
            unknown = serialized_topology(principal=READ, lens="not-a-lens")
            whole = serialized_topology(principal=READ)
        self.assertEqual(applied["lens"], "unobserved-resources")
        self.assertEqual([i["name"] for i in applied["lenses"]],
                         [lens.name for lens in topology_lenses()])
        self.assertLess(applied["summary"]["nodes"], whole["summary"]["nodes"])
        # An unrecognized lens is the whole graph, said out loud.
        self.assertIsNone(unknown["lens"])
        self.assertEqual(unknown["summary"], whole["summary"])

    def test_a_lens_costs_no_extra_query(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            with CaptureQueriesContext(database_connection) as whole:
                serialized_topology(principal=READ)
            with CaptureQueriesContext(database_connection) as narrowed:
                serialized_topology(principal=READ, lens="unobserved-resources")
        self.assertEqual(len(narrowed), len(whole))

    def test_discovery_offers_every_lens_and_withholds_them_without_read(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            offered = command_center("", principal=READ)["views"]
            denied = command_center("", principal=NONE)["views"]
            matched = command_center("ability governs", principal=READ)["views"]
        self.assertEqual([i.name for i in offered], [lens.name for lens in topology_lenses()])
        self.assertEqual(denied, ())
        self.assertEqual([i.name for i in matched], ["ungoverned-resources"])
