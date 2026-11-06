"""The topology pages: the whole map, one node in focus, its actions and its lenses."""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import connection as database_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from hq.domains.control_plane.topology_views import TopologyView
from hq.platform.application.derivation_testing import uncached

from ..action_links import topology_investigation_links, topology_url
from ..command_center import command_center
from ..connections import ConnectionAbility, ConnectionInstance, ConnectionSpec
from ..plugin_testing import offering
from ..security import Capability
from ..topology import derive_topology, topology as serialized_topology
from ..topology_lenses import apply_lens, lens_for, topology_lenses
from .host_only import host_only
from .markup import hrefs, select, texts, within
from .topology_principals import MANAGE, NONE, READ


class TopologyPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="not-a-real-password")
        self.client.force_login(self.user)
        ManagedResource.objects.create(
            key="internal-name",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

    def test_page_is_progressively_enhanced_and_actions_remain_real_forms(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        with host_only():
            response = self.client.get(reverse("control_plane:topology"))

        self.assertContains(response, "data-topology")
        # Without script, a node's details are one link away: focusing it
        # draws them.
        self.assertIn(TopologyView._focus_link("resource:internal-name"), hrefs(response))
        self.assertNotContains(response, "<script>")

        focused = self.page(focus="resource:internal-name")
        self.assertContains(
            focused,
            f'action="{reverse("control_plane:reconcile", kwargs={"key": "internal-name"})}"',
        )

    def test_the_page_carries_summaries_and_fetches_a_body_when_opened(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        response = self.page()

        reconcile = reverse("control_plane:reconcile", kwargs={"key": "internal-name"})
        self.assertFalse(select(response, "formaction", reconcile))
        body_url = next(
            item["body_url"]
            for group in response.context["topology_groups"]
            for item in group["items"]
            if item["node"].id == "resource:internal-name"
        )
        self.assertTrue(select(response, "data-topology-body", body_url))

        body = self.body("resource:internal-name")
        self.assertEqual(body.status_code, 200)
        # The body's post action names the page's one post form, which carries
        # the CSRF token, so a fetched body posts exactly like a drawn one.
        (button,) = select(body, "formaction", reconcile)
        self.assertEqual(button.attrs["form"], "hq-post")
        self.assertTrue(select(response, "id", "hq-post"))
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_only_a_node_the_projection_holds_is_opened_or_focused(self):
        self.assertEqual(self.body("resource:not-declared").status_code, 404)
        self.assertEqual(self.page(focus="not-a-node").context["focus_node"], "")

    def test_a_body_needs_a_signed_in_operator(self):
        self.client.logout()

        self.assertEqual(self.body("resource:internal-name").status_code, 302)

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

    def test_a_node_title_links_to_the_thing_it_names(self):
        """A node carries a URL; a title that renders as text throws it away."""

        response = self.page()

        detail = reverse("control_plane:detail", kwargs={"key": "internal-name"})
        self.assertIn(detail, hrefs(response))
        # A multi-line {# … #} is not a comment in Django, it is text. One
        # holding the word <strong> renders an element, and the explorer reads
        # the first <strong> in a node as its title, so the leak is silent
        # until the status line starts quoting the commentary.
        self.assertNotContains(response, "{#")
        self.assertNotContains(response, "#}")

    def test_records_of_one_name_and_type_are_told_apart_by_their_values(self):
        for key, content in (
            ("caa-issue", '0 issue "ca.example.net"'),
            ("caa-iodef", '0 iodef "mailto:a@example.com"'),
        ):
            ManagedResource.objects.create(
                key=key,
                kind="cloudflare.dns_record",
                spec={"zone": "example.com", "name": "example.com", "record_type": "CAA", "content": content},
            )

        response = self.page(all="1")

        def values(node):
            return [
                element.text
                for element in within(response, "data-topology-node", node)
                if "data-topology-node-summary" in element.attrs
            ]

        (issue,) = values("resource:caa-issue")
        (iodef,) = values("resource:caa-iodef")
        self.assertIn("ca.example.net", issue)
        self.assertIn("mailto:a@example.com", iodef)
        # A card no other card shares a name with is told apart by its name.
        self.assertEqual(values("resource:internal-name"), [])

    def test_clear_selection_is_drawn_hidden_when_nothing_is_selected(self):
        (reset,) = select(self.page(), "data-topology-reset")

        self.assertIn("hidden", reset.attrs)

    def test_a_node_states_each_edge_from_where_it_stands(self):
        """Direction is the half of an edge a neighbor list throws away."""

        response = self.page(all=1)

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

        # The same edge renders at both ends, and each row walks to the other.
        resource, ability = self.body("resource:internal-name"), self.body(ability_id)
        self.assertIn(TopologyView._focus_link(ability_id), hrefs(resource))
        self.assertIn(TopologyView._focus_link("resource:internal-name"), hrefs(ability))
        # The kind by its label, never its identifier.
        self.assertContains(ability, "Internal DNS record")
        self.assertNotContains(ability, ">adguard.rewrite<")

    def test_a_node_body_carries_the_triage_the_projection_already_derived(self):
        """Declared versus observed is the whole of triage, so the node body
        carries both.
        """

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
        ManagedResource.objects.create(
            key="example-stack",
            kind="portainer.stack",
            spec={"name": "example-stack", "host": "example-host"},
        )

        response = self.body("resource:behind-name")
        disabled = self.body("resource:disabled-name")
        unswept = self.body("resource:example-stack")

        # A comparison, not two raw revision numbers.
        self.assertNotContains(response, "evision")
        # The field the reading declined to echo back.
        self.assertIn("answer", texts(response, "code"))
        # Age, not an ISO timestamp.
        self.assertContains(response, "3\xa0hours ago")
        # A kind a sweep reads is not observed yet; one no sweep reads never is.
        self.assertNotContains(disabled, "HQ does not read this.")
        self.assertContains(unswept, "HQ does not read this.")

    def test_the_whole_map_leaves_out_quiet_lanes_and_unlinked_cards_until_asked(self):
        from ..topology_lenses import QUIET_KINDS

        shown = self.page()
        everything = self.page(all=1)

        def cards(response):
            return [item for group in response.context["topology_groups"] for item in group["items"]]

        self.assertTrue(all(item["degree"] and item["node"].kind not in QUIET_KINDS for item in cards(shown)))
        left_out = shown.context["topology_left_out"]
        self.assertGreater(left_out, 0)
        self.assertEqual(len(cards(everything)), len(cards(shown)) + left_out)
        # "N of M shown" is out of every card, the ones left out included.
        self.assertEqual(shown.context["topology_total"], len(cards(everything)))
        self.assertEqual(shown.context["topology_shown"], len(cards(shown)))
        self.assertTrue(select(shown, "data-topology-total", str(len(cards(everything)))))

    def test_a_view_shows_everything_it_holds(self):
        from ..topology_lenses import topology_lenses

        for lens in topology_lenses():
            with self.subTest(lens=lens.name):
                self.assertEqual(self.page(lens=lens.name).context["topology_left_out"], 0)

    def test_a_node_nothing_reaches_says_so_rather_than_drawing_an_empty_box(self):
        alone = next(
            item["node"].id
            for group in self.page(all=1).context["topology_groups"]
            for item in group["items"]
            if not item["degree"]
        )

        self.assertContains(self.body(alone), "Connected to nothing.")


class FocusedPageTests(TestCase):
    """A focused node's details, drawn once and readable at any width."""

    def setUp(self):
        from .test_relationships import estate, login

        estate(2)
        login(self.client)

    def page(self, focus):
        with host_only():
            return self.client.get(reverse("control_plane:topology"), {"focus": focus})

    def test_the_details_render_once_in_the_panel_ordered_by_importance(self):
        response = self.page("zone:example.com")
        body = response.content.decode()

        self.assertEqual(len(select(response, "data-topology-body-of", "zone:example.com")), 1)
        panel = within(response, "id", "topology-detail")
        self.assertTrue(any(element.attrs.get("data-topology-body-of") == "zone:example.com" for element in panel))
        # Each value says when it was read, and no list of links is comma-joined.
        self.assertTrue(any(element.tag == "time" and element.attrs.get("datetime") for element in panel))
        self.assertNotRegex(body, r"</a>,\s*<a")
        detail = response.context["topology_detail"]
        for direction in ("in", "out"):
            ranks = [row["rank"] for row in detail["relations"] if row["direction"] == direction]
            self.assertEqual(ranks, sorted(ranks))

    def test_the_trace_ledger_is_drawn_only_with_relationships_in_it(self):
        related = self.page("zone:example.com")
        self.assertTrue(select(related, "data-topology-edge"))

        with host_only():
            unfocused = self.client.get(reverse("control_plane:topology"), {"all": 1})
        lonely = next(
            item["node"].id
            for group in unfocused.context["topology_groups"]
            for item in group["items"]
            if not item["degree"]
        )
        alone = self.page(lonely)
        self.assertEqual(alone.context["focus_node"], lonely)
        self.assertFalse(select(alone, "data-topology-edge"))
        self.assertFalse(select(alone, "id", "topology-relations"))

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
        self.assertContains(response, "2\xa0hours ago")
        self.assertNotContains(response, "HQ does not read this.")

    def test_a_card_says_what_its_number_counts(self):
        response = self.page("zone:example.com")
        degree = next(
            item["degree"]
            for group in response.context["topology_groups"]
            for item in group["items"]
            if item["node"].id == "zone:example.com"
        )

        noun = "link" if degree == 1 else "links"
        self.assertTrue(select(response, "aria-label", f"Details for example.com, {degree} {noun}"))

    def project(self):
        with host_only():
            return derive_topology(principal=READ)


class ConnectionActionTests(TestCase):
    """A connection offers what its own spec declared, resolved generically."""

    def spec(self, **overrides) -> ConnectionSpec:
        return ConnectionSpec(
            "example.declared",
            "Declared routes",
            "A synthetic connection family that declares where it can be reached.",
            Capability.READ,
            lambda: (ConnectionInstance("one", "One", "example", "good", "Healthy"),),
            **overrides,
        )

    def project(self, spec, principal=READ):
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
        ability = next(n for n in self.project(spec, READ).nodes if n.id == "ability:example.declared:example.rotate")

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
        ManagedResource.objects.create(
            key="observed-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "example-cloudflare"},
        )
        self.unobserved = ManagedResource.objects.create(
            key="lonely-record", kind="adguard.rewrite", spec={"domain": "app.example.test", "answer": "192.0.2.10"}
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

    def project(self):
        with host_only():
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
            key="swept-record", kind="adguard.rewrite", spec={"domain": "fresh.example.test", "answer": "192.0.2.20"}
        )
        skipped = ManagedResource.objects.create(
            key="skipped-record",
            kind="adguard.rewrite",
            spec={"domain": "skipped.example.test", "answer": "192.0.2.21"},
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
            applied = serialized_topology(principal=READ, lens="unobserved-resources")
            whole = serialized_topology(principal=READ)
        self.assertEqual(applied["lens"], "unobserved-resources")
        self.assertLess(applied["summary"]["nodes"], whole["summary"]["nodes"])

    def test_a_lens_costs_no_extra_query(self):
        with (
            host_only(),
            uncached(),
        ):
            with CaptureQueriesContext(database_connection) as whole:
                serialized_topology(principal=READ)
            with CaptureQueriesContext(database_connection) as narrowed:
                serialized_topology(principal=READ, lens="unobserved-resources")
        self.assertEqual(len(narrowed), len(whole))

    def test_discovery_offers_every_lens_and_withholds_them_without_read(self):
        with host_only():
            offered = command_center("", principal=READ)["views"]
            denied = command_center("", principal=NONE)["views"]
            matched = command_center("ungoverned", principal=READ)["views"]
        self.assertEqual([i.name for i in offered], [lens.name for lens in topology_lenses()])
        self.assertEqual(denied, ())
        self.assertEqual([i.name for i in matched], ["ungoverned-resources"])
