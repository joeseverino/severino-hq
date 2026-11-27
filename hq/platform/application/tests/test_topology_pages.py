"""The topology pages: the whole map, one node in focus, its actions and its lenses."""

from datetime import datetime, timedelta

from django.db import connection as database_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.observations.r2 import BACKUP_KIND
from hq.platform.application.derivation_testing import uncached
from hq.platform.application.inventory_testing import store
from hq.platform.core.testing import NOBODY, READER, connection, declare, operator

from ..action_links import topology_investigation_links, topology_url
from ..command_center import command_center
from ..connection_catalog import CONTROLLER_CONNECTIONS
from ..connections import ConnectionAbility, ConnectionInstance, ConnectionSpec
from ..plugin_testing import offering
from ..references import Kind
from ..security import Capability
from ..topology import derive_topology, topology as serialized_topology
from ..topology_lenses import apply_lens, lens_for, topology_lenses
from ..topology_model import observable
from .host_only import host_only
from .topology_principals import MANAGE


class TopologyPageTests(TestCase):
    def setUp(self):
        self.user = operator(self.client)
        declare("internal-name", "adguard.rewrite", {"domain": "app.example.test", "answer": "192.0.2.10"})

    def test_only_a_node_the_projection_holds_is_opened_or_focused(self):
        self.assertEqual(self.body("resource:not-declared").status_code, 404)
        self.assertEqual(self.page(focus="not-a-node").context["focus_node"], "")

    def test_focus_becomes_a_shareable_bounded_trace(self):
        with host_only():
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

    def page(self, **params):
        with host_only():
            return self.client.get(reverse("control_plane:topology"), params)

    def body(self, node_id):
        with host_only():
            return self.client.get(reverse("control_plane:topology_node"), {"node": node_id})

    def test_a_node_states_each_edge_from_where_it_stands(self):
        """Direction is the half of an edge a neighbor list throws away."""

        response = self.page(lens="everything")

        ability_id = "ability:infrastructure.controllers:adguard.rewrite"
        relations = {
            item["node"].id: item["relations"]
            for group in response.context["topology_groups"]
            for item in group["items"]
        }
        self.assertEqual(
            [(row["direction"], row["other"].id) for row in relations["resource:internal-name"]],
            [("in", "service:app.example.test"), ("in", ability_id)],
        )
        self.assertEqual(
            [(row["direction"], row["other"].id) for row in relations[ability_id]],
            [("out", "resource:internal-name")],
        )

    def test_only_a_kind_no_sweep_reads_is_never_observed(self):
        """A kind a sweep reads is not observed yet, and a reading ability is read when its kind was."""

        declare(
            "disabled-name", "adguard.rewrite", {"domain": "off.example.test", "answer": "192.0.2.12"}, enabled=False
        )
        declare("example-authority", "pki.authority", {"name": "Example Root"})
        store(BACKUP_KIND, {"connection_ref": "example-backups", "key": "example.tar.gz"}, age=timedelta(hours=3))
        with host_only():
            nodes = {node.id: node for node in derive_topology(principal=READER).nodes}

        self.assertTrue(observable(nodes["resource:disabled-name"]))
        self.assertFalse(observable(nodes["resource:example-authority"]))
        ability = nodes[Kind.ABILITY.of(f"{CONTROLLER_CONNECTIONS}:{BACKUP_KIND}")]
        self.assertTrue(observable(ability))
        self.assertTrue(ability.observed_at)

    def test_the_whole_map_leaves_out_quiet_lanes_and_unlinked_cards_until_asked(self):
        from ..topology_lenses import QUIET_KINDS

        shown = self.page()
        everything = self.page(lens="everything")

        def cards(response):
            return [item for group in response.context["topology_groups"] for item in group["items"]]

        self.assertTrue(all(item["degree"] and item["node"].kind not in QUIET_KINDS for item in cards(shown)))
        self.assertTrue(shown.context["topology_held"])
        # "N of M shown" is out of every card, the ones held back included.
        self.assertLess(len(cards(shown)), len(cards(everything)))
        self.assertEqual(shown.context["topology_total"], len(cards(everything)))
        self.assertEqual(shown.context["topology_shown"], len(cards(shown)))

    def test_a_view_shows_everything_it_holds(self):
        from ..topology_lenses import topology_lenses

        for lens in topology_lenses():
            with self.subTest(lens=lens.name):
                context = self.page(lens=lens.name).context
                self.assertEqual((context["topology_held"], context["topology_shown"]), ((), context["topology_total"]))


class FocusedPageTests(TestCase):
    """A focused node's details."""

    def setUp(self):
        from .test_relationships import estate, login

        estate(2)
        login(self.client)

    def page(self, focus):
        with host_only():
            return self.client.get(reverse("control_plane:topology"), {"focus": focus})

    def test_a_focused_node_s_relations_are_ordered_by_importance(self):
        response = self.page("zone:example.com")
        detail = response.context["topology_detail"]
        for direction in ("in", "out"):
            ranks = [row["rank"] for row in detail["relations"] if row["direction"] == direction]
            self.assertEqual(ranks, sorted(ranks))

    def test_a_domain_is_observed_when_its_zone_reading_is(self):
        from hq.domains.control_plane.models import ProviderInventory

        ProviderInventory.objects.exclude(kind="cloudflare.zone").delete()
        read = timezone.now() - timedelta(hours=2)
        ProviderInventory.objects.filter(kind="cloudflare.zone").update(observed_at=read)

        node = next(node for node in self.project().nodes if node.id == "zone:example.com")
        self.assertEqual(datetime.fromisoformat(node.observed_at), read)

        response = self.page("zone:example.com")
        detail = response.context["topology_detail"]
        self.assertEqual(detail["observed_age"], read)

    def project(self):
        with host_only():
            return derive_topology(principal=READER)


class ConnectionActionTests(TestCase):
    """A connection offers what its own spec declared, resolved generically."""

    def spec(self, **overrides) -> ConnectionSpec:
        return ConnectionSpec(
            "example.declared",
            "Declared routes",
            "A synthetic connection family that declares where it can be reached.",
            Capability.READ,
            lambda: (ConnectionInstance("one", "One", "example", "good", "Healthy", credential_model="none"),),
            **overrides,
        )

    def project(self, spec, principal=READER):
        with offering(spec, alone=True):
            return derive_topology(principal=principal)

    def node(self, spec):
        return next(n for n in self.project(spec).nodes if n.id == "connection:example.declared:one")

    def test_topology_investigations_share_one_addressing_contract(self):
        node_id = "connection:example.declared:one"

        actions = topology_investigation_links(node_id)

        self.assertEqual(
            [(action.name, action.url) for action in actions],
            [
                ("topology", topology_url(node_id)),
                (
                    "impact",
                    topology_url(node_id, direction="outbound", depth=3),
                ),
            ],
        )
        self.assertEqual(
            topology_url(node_id, lens="operations"),
            f"{reverse('control_plane:topology')}?focus=connection%3Aexample.declared%3Aone&lens=operations#trace",
        )

    def test_every_declared_route_becomes_its_own_action(self):
        node = self.node(
            self.spec(
                management_route="control_plane:list",
                setup_route="control_plane:create",
                documentation_url="https://docs.example.test/connections",
            )
        )
        self.assertEqual(
            [(a.name, a.url) for a in node.actions],
            [
                ("open", reverse("control_plane:connections")),
                ("manage", reverse("control_plane:list")),
                ("set_up", reverse("control_plane:create")),
                ("documentation", "https://docs.example.test/connections"),
            ],
        )

    def test_the_same_route_twice_is_offered_once(self):
        node = self.node(self.spec(management_route="control_plane:connections"))
        self.assertEqual([a.name for a in node.actions], ["open"])

    def test_a_spec_web_route_replaces_the_default_destination(self):
        node = self.node(self.spec(web_route="control_plane:list"))
        self.assertEqual(node.url, reverse("control_plane:list"))

    def test_an_ability_naming_a_capability_reports_the_canonical_contract(self):
        spec = self.spec(
            abilities=(
                ConnectionAbility(
                    "example.rotate",
                    "Rotate",
                    "Rotate the credential this connection carries.",
                    effect="infrastructure_change",
                    capability="infrastructure.resource.reconcile",
                ),
            )
        )
        ability = next(n for n in self.project(spec, MANAGE).nodes if n.id == "ability:example.declared:example.rotate")
        command = next(a for a in ability.actions if a.name == "command")
        self.assertEqual(command.capability, "infrastructure.resource.reconcile")
        self.assertEqual(command.effect, "infrastructure_change")

    def test_an_ability_never_advertises_a_command_the_principal_cannot_run(self):
        spec = self.spec(
            abilities=(
                ConnectionAbility(
                    "example.rotate",
                    "Rotate",
                    "Rotate the credential this connection carries.",
                    effect="infrastructure_change",
                    capability="infrastructure.resource.reconcile",
                ),
            )
        )
        ability = next(n for n in self.project(spec, READER).nodes if n.id == "ability:example.declared:example.rotate")

        self.assertEqual([a.name for a in ability.actions], ["focus"])

    def test_an_ability_without_a_capability_only_relates(self):
        spec = self.spec(
            abilities=(ConnectionAbility("example.inspect", "Inspect", "Read what this connection sees."),)
        )
        ability = next(n for n in self.project(spec).nodes if n.id == "ability:example.declared:example.inspect")
        self.assertEqual([a.name for a in ability.actions], ["focus"])


class TopologyLensTests(TestCase):
    """A lens narrows the one projection; it never derives a second one."""

    def setUp(self):
        declare("observed-zone", "cloudflare.zone", {"zone": "example.com", "connection_ref": "example-cloudflare"})
        self.unobserved = declare(
            "lonely-record", "adguard.rewrite", {"domain": "app.example.test", "answer": "192.0.2.10"}
        )
        connection(
            "cloudflare_dns",
            "example-cloudflare",
            endpoint="https://api.example.test/client/v4",
            reaches=["example.com"],
        )

    def project(self):
        with host_only():
            return derive_topology(principal=READER)

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
        declare("unclaimed-kind", "example.unclaimed")
        selected = {n.id for n in self.narrowed("ungoverned-resources").nodes}
        self.assertIn("resource:unclaimed-kind", selected)
        self.assertNotIn("resource:observed-zone", selected)

    def test_a_lens_finds_what_a_sweep_skipped_while_confirming_siblings(self):
        """Health describes the last observation's content, never its age."""
        now = timezone.now()
        fresh = declare("swept-record", "adguard.rewrite", {"domain": "fresh.example.test", "answer": "192.0.2.20"})
        skipped = declare(
            "skipped-record", "adguard.rewrite", {"domain": "skipped.example.test", "answer": "192.0.2.21"}
        )
        ManagedResource.objects.filter(pk=fresh.pk).update(last_observed_at=now)
        ManagedResource.objects.filter(pk=skipped.pk).update(last_observed_at=now - timedelta(days=4))
        ProviderInventory.objects.create(kind="adguard.rewrite", observed_at=now)
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

    def test_serialization_names_the_applied_lens(self):
        with host_only():
            applied = serialized_topology(principal=READER, lens="unobserved-resources")
            whole = serialized_topology(principal=READER)
        self.assertEqual(applied["lens"], "unobserved-resources")
        self.assertLess(applied["summary"]["nodes"], whole["summary"]["nodes"])

    def test_a_lens_costs_no_extra_query(self):
        with (
            host_only(),
            uncached(),
        ):
            with CaptureQueriesContext(database_connection) as whole:
                serialized_topology(principal=READER)
            with CaptureQueriesContext(database_connection) as narrowed:
                serialized_topology(principal=READER, lens="unobserved-resources")
        self.assertEqual(len(narrowed), len(whole))

    def test_discovery_offers_every_lens_and_withholds_them_without_read(self):
        with host_only():
            offered = command_center("", principal=READER)["views"]
            denied = command_center("", principal=NOBODY)["views"]
            matched = command_center("ungoverned", principal=READER)["views"]
        self.assertEqual([i.name for i in offered], [lens.name for lens in topology_lenses()])
        self.assertEqual(denied, ())
        self.assertEqual([i.name for i in matched], ["ungoverned-resources"])
