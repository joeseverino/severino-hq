"""The web shell's pages: search, lists, the dashboard and the navigation."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import call, patch

from django.urls import reverse
from django.utils import timezone

from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ProviderInventory,
)
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.action_links import command_url
from hq.platform.application.asks import REFRESHING
from hq.platform.application.cadence_testing import controller_arrived
from hq.platform.application.labels import DOT
from hq.platform.application.markup_testing import elements, hrefs, select, text, with_class
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.resources import get_resource
from hq.platform.application.tests.glance_readings import HOST, keep_machine, reported_machine
from hq.platform.application.tests.host_only import host_only
from hq.platform.core.testing import READER, SignedInTestCase, connection, declare
from hq_sdk.testing import plain


class SearchPageTests(SignedInTestCase):
    def test_search_with_nothing_asked_is_a_search_box(self):
        with patch("hq.platform.core.views.command_center") as discovery:
            response = self.client.get("/search/")

        discovery.assert_not_called()
        self.assertTrue(select(response, "name", "q"))
        self.assertFalse([url for url in hrefs(response) if url.startswith(command_url("project.create")[:10])])

    def test_a_query_finds_pages_and_commands_by_their_names_and_shows_no_identifier(self):
        response = self.client.get("/search/", {"q": "project"})

        links = hrefs(response)
        self.assertIn(command_url("project.create"), links)
        self.assertIn(reverse("projects:list"), links)
        self.assertNotIn("project.create", text(response))

    def test_a_place_found_is_a_row_with_its_state_and_a_page_is_its_name(self):
        from hq.platform.application.command_center import DiscoveryItem
        from hq.platform.core.views import DISCOVERY_GROUPS

        def item(kind, label, summary, badges):
            return DiscoveryItem(kind, label.lower(), label, summary, f"/example/{kind}/", label, badges)

        found = {
            **dict.fromkeys(DISCOVERY_GROUPS, ()),
            "estate": (item("machine", "Example host", "An example machine", ("Running", "")),),
            "resources": (item("page", "Example page", "What the example page lists", ()),),
        }
        with patch("hq.platform.core.views.command_center", return_value=found):
            response = self.client.get("/search/", {"q": "example"})

        shown = text(response)
        self.assertLessEqual({"/example/machine/", "/example/page/"}, set(hrefs(response)))
        self.assertIn("An example machine", shown)
        self.assertIn("Running", shown)
        self.assertNotIn("What the example page lists", shown)

    def test_a_machine_found_carries_its_state_as_a_chip(self):
        declare("example-host", "machine", {"name": "example-host"})

        response = self.client.get("/search/", {"q": "example-host"})

        self.assertIn("/infrastructure/machines/example-host/", hrefs(response))
        self.assertIn("not monitored", [chip.words for chip in with_class(response, "tag")])

    def test_query_filters_commands_as_well_as_records(self):
        response = self.client.get("/search/", {"q": "certificate.renew"})

        links = hrefs(response)
        self.assertIn(command_url("certificate.renew"), links)
        self.assertNotIn(command_url("project.create"), links)

    def test_palette_is_a_small_fragment_with_local_record_search(self):
        with (
            patch(
                "hq.platform.core.views.global_search",
                return_value={"groups": [], "total": 0},
            ) as records,
            patch("hq.domains.contacts.inbox.search") as contacts,
        ):
            response = self.client.get(
                "/search/",
                {"q": "certificate"},
                HTTP_X_FRAGMENT="palette",
            )

        self.assertTrue(select(response, "data-command-center-results"))
        self.assertFalse(elements(response, "html"))
        records.assert_called_once()
        self.assertEqual(records.call_args.args, ("certificate",))
        self.assertEqual(records.call_args.kwargs["limit_per_scope"], 3)
        contacts.assert_not_called()

    def test_the_empty_palette_offers_every_page_and_nothing_to_do(self):
        response = self.client.get("/search/", HTTP_X_FRAGMENT="palette")

        offered = [option.attrs["href"] for option in select(response, "data-command-center-option")]
        self.assertIn(reverse("projects:list"), offered)
        self.assertIn(reverse("control_plane:connections"), offered)
        self.assertFalse([url for url in offered if url.startswith(command_url("project.create")[:10])])

    def test_palette_includes_indexed_records_beside_destinations(self):
        project = Project.objects.create(
            name="Palette record target",
            description="A uniquely indexed command center result.",
        )

        response = self.client.get(
            "/search/",
            {"q": "Palette record target"},
            HTTP_X_FRAGMENT="palette",
        )

        self.assertIn(project.get_absolute_url(), hrefs(response))

    def test_palette_searches_content_not_only_navigation(self):
        item = ContentItem.objects.create(
            title="Palette content target",
            notes="A uniquely indexed piece of content.",
        )

        response = self.client.get(
            "/search/",
            {"q": "Palette content target"},
            HTTP_X_FRAGMENT="palette",
        )

        self.assertIn(item.get_absolute_url(), hrefs(response))

    def test_authenticated_pages_ship_the_global_palette_progressively(self):
        response = self.client.get("/projects/")

        self.assertTrue(select(response, "id", "modal-command-center"))
        (form,) = select(response, "data-command-center-form")
        self.assertEqual((form.tag, form.attrs["action"]), ("form", reverse("search")))
        opener = select(response, "data-command-center-open")
        self.assertTrue(opener)
        self.assertTrue(all(each.attrs.get("aria-controls") == "modal-command-center" for each in opener))

    def test_results_highlight_matches_and_skip_empty_scopes(self):

        Project.objects.create(
            name="Highlight target",
            description="global search rendering check",
        )
        response = self.client.get("/search/", {"q": "highlight"})

        self.assertIn("Highlight", " ".join(mark.text for mark in elements(response, "mark")))
        # Empty scopes are omitted entirely, not rendered as empty cards.
        shown = {
            each.attrs["id"]
            for each in select(response, "aria-labelledby")
            if each.attrs.get("id", "").startswith("scope-")
        }
        self.assertEqual(shown, {section["id"] for section in response.context["result_sections"]})
        self.assertEqual(
            {section["id"] for section in response.context["result_sections"]},
            {f"scope-{group['scope']}" for group in response.context["groups"] if group["count"]},
        )

    def test_a_hit_quotes_its_records_text_and_each_group_is_a_way_from_the_navigation(self):
        """What a record holds is shown as written, however unlike HQ's own
        words, and every group of results is one link over them."""

        Project.objects.create(name="Quoted target", description="enabled: False at 2026-11-02 09:30, quoted")
        ContentItem.objects.create(title="Quoted content", notes="A quoted piece of content.")
        response = self.client.get("/search/", {"q": "quoted"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("2026-11-02 09:30", text(response))
        ids = [section["id"] for section in response.context["result_sections"]]
        self.assertGreater(len(ids), 1)
        self.assertEqual(
            [link.attrs["href"] for link in select(response, "data-page-nav-link")], [f"#{i}" for i in ids]
        )


class EmptyListTests(SignedInTestCase):
    def test_a_list_emptied_by_its_filter_offers_the_way_back(self):
        page = self.client.get(reverse("expenses:list"), {"q": "nothing-matches-this"})

        self.assertTrue(select(page, "data-empty-state"))
        self.assertIn(page.context["table"]["clear"].url, hrefs(page))
        self.assertFalse(elements(page, "table"))


class KeyColumnTests(SignedInTestCase):
    """A list says which columns a phone keeps."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # An empty list draws no table, so its headings need rows under them.
        from hq.platform.core import example_estate
        from hq.platform.core.example_estate.testing import throwaway_media

        with throwaway_media():
            example_estate.seed()

    def test_every_shared_list_names_at_least_one_key_column(self):
        """With none, a phone would show only the column that names the row."""

        for name in ("expenses:list", "projects:list", "content:list", "assets:list", "core:audit_list"):
            with self.subTest(page=name):
                headings = elements(self.client.get(reverse(name)), "th")
                self.assertTrue(any("key-col" in heading.attrs.get("class", "").split() for heading in headings))


class DashboardWorkflowTests(SignedInTestCase):
    def test_dashboard_and_machine_page_share_the_machine_observation(self):
        observed = timezone.now()
        machine = declare("example-host", "machine", {"name": "example-host"})
        keep_machine(reported_machine("example-host", host=HOST), at=observed)
        DashboardMachine.objects.create(machine=machine)

        dashboard = self.client.get("/")
        load = get_resource("machines", "example-host", principal=READER)["load"]

        self.assertIn("9%", text(dashboard))
        self.assertIn("9%", [metric["value"] for metric in load["metrics"]])

    def test_dashboard_refresh_is_one_explicit_controller_request(self):
        machine = declare("example-host", "machine", {"name": "example-host"})
        DashboardMachine.objects.create(machine=machine)
        connection(
            "portainer",
            "example-portainer",
            controller_id="example-host",
            endpoint="https://portainer.example.test",
            reaches=["example-host"],
        )

        with (
            patch("hq.platform.application.cadence._ring") as ring,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                "/dashboard/glance/", form_keyed(), headers={"X-Requested-With": "XMLHttpRequest"}
            )

        self.assertEqual((response.status_code, response.json()["live"]), (202, True))
        # A person pressed for it, so its panel says it is under way.
        self.assertContains(self.client.get("/dashboard/glance/"), REFRESHING)
        self.assertEqual(
            list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)),
            [f"machine-{machine.pk}"],
        )
        self.assertEqual(ring.call_args_list.count(call("glance")), 1)

    def test_dashboard_weather_settings_are_managed_in_the_ui(self):
        response = self.client.post(
            "/dashboard/glance/settings/",
            form_keyed(
                {
                    "infrastructure_label": "Server room",
                    "weather_point": "41.1, -87.2",
                    "weather_label": "Outside",
                }
            ),
        )

        self.assertRedirects(response, "/")
        configuration = DashboardConfiguration.objects.get()
        self.assertEqual(configuration.infrastructure_label, "Server room")
        self.assertEqual(configuration.weather_point, "41.1000,-87.2000")
        self.assertEqual(configuration.weather_label, "Outside")

    def test_action_items_is_the_full_existing_queue_and_filters_it(self):
        ContentItem.objects.create(title="Unfinished post", status=ContentItem.Status.DRAFT)

        response = self.client.get("/action-items/")
        filtered = self.client.get("/action-items/", {"status": "serious", "q": "draft"})

        drafts = f"{reverse('content:list')}?status=draft"
        self.assertIn(drafts, hrefs(response))
        # Filtered to urgent only, so an entry that is not urgent is gone even
        # though the query still matches its text.
        self.assertNotIn(drafts, hrefs(filtered))

    def test_profile_count_is_lazy_off_dashboard(self):
        Expense.objects.create(date=date(2026, 3, 1), vendor="Example Registrar", total_cost=Decimal("12.00"))

        page = self.client.get("/projects/")
        with host_only():
            count = self.client.get("/action-items/count/")

        (badge,) = select(page, "data-action-count")
        self.assertIn("hidden", badge.attrs)
        self.assertIn("data-fragment-load", badge.attrs)
        self.assertEqual(badge.attrs["data-fragment"], "/action-items/count/")
        # The answer is the same region, counted, and asks for nothing more.
        (answer,) = select(count, "data-action-count")
        self.assertEqual(answer.attrs["id"], badge.attrs["id"])
        self.assertNotIn("data-fragment", answer.attrs)
        self.assertNotIn("hidden", answer.attrs)
        self.assertEqual(count.context["profile_action_count"], 1)

    def test_the_controllers_dot_says_only_what_is_known(self):
        """Green while it is arriving, the attention color once it has gone
        quiet, and neither where it has never run."""

        never = self.client.get("/")
        controller_arrived()
        arriving = self.client.get("/")
        controller_arrived(ago=timedelta(hours=1))
        silent = self.client.get("/")

        states = {
            name: (page.context["controller"].known, page.context["controller"].silent)
            for name, page in (("never", never), ("arriving", arriving), ("silent", silent))
        }
        self.assertEqual(states, {"never": (False, False), "arriving": (True, False), "silent": (True, True)})
        self.assertIn(reverse("control_plane:controller"), hrefs(silent))

    def test_the_dashboard_draws_its_contacts_without_waiting_on_d1(self):
        rows = [
            {"id": 7, "name": "One call", "status": "unread", "created_at": "2026-08-23", "database_id": "example"},
            *({"id": number, "status": "unread", "database_id": "example"} for number in (6, 5)),
        ]
        ProviderInventory.objects.create(kind="cloudflare.contact_submission", records=rows, observed_at=timezone.now())
        page = self.client.get("/")

        # With the page, from the stored rows: nothing is fetched afterwards,
        # so no card appears and then changes.
        self.assertEqual(page.context["unread_contacts_count"], 3)
        self.assertIn("One call", text(page))
        # Inside the page itself; the connection dialog every page carries
        # reads only when it is opened.
        main = page.content.decode().split("<main", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("data-fragment-load", main)

    def test_an_empty_contact_feed_draws_no_card(self):
        page = self.client.get("/")

        self.assertFalse(page.context["recent_contacts"])

    def test_the_dashboard_says_how_this_request_reached_it(self):
        """The reading the connection panel opens on, and a link to that panel."""

        page = self.client.get("/")
        panel = self.client.get(reverse("connection"))

        found = page.context["request_connection"]
        self.assertEqual(found.summary, panel.context["connection"].summary)
        # Its value opens the panel, and is a plain link to it without script;
        # the lines under it say the summary's parts, the dots between them drawn, never read.
        (badge, fact) = next(
            (each, up)
            for each in select(page, "data-modal-open", "connection")
            for up in each.ancestors
            if "fact" in up.classes
        )
        self.assertEqual(badge.attrs["href"], reverse("connection"))
        self.assertTrue(all(plain(part) in plain(fact.text) for part in found.summary.split(DOT)), fact.text)
        self.assertIn(found.transport, fact.text)

    def test_dashboard_routes_infrastructure_findings_to_their_evidence(self):
        declare(
            "example-wildcard",
            "tls.certificate",
            generation=2,
            observed_generation=1,
            conditions=[
                {
                    "type": "Degraded",
                    "status": True,
                    "reason": "ExpiringSoon",
                    "message": "Certificate expires tomorrow.",
                }
            ],
        )

        response = self.client.get("/action-items/")

        # Each finding is its own item, linked to its own rule's evidence.
        rule_links = f"{reverse('control_plane:findings')}?rule="
        self.assertTrue([url for url in hrefs(response) if url.startswith(rule_links)])

    def test_output_a_project_lacks_is_counted_but_not_queued(self):
        """A shape of the portfolio, not a decision waiting on anyone.

        Nothing clears "active work with nothing written about it yet" except
        months of work, so as a queue entry it would be permanently the largest and
        permanently unactionable. The count still shows on the projects card.
        """

        Project.objects.create(name="Documentable lab", status=Project.Status.ACTIVE)

        response = self.client.get("/")

        self.assertIn("1 has no writeup or doc", text(response))
        queue = self.client.get(reverse("action_items"))
        self.assertNotIn(f"{reverse('projects:list')}?needs_output=1", hrefs(queue))

    def test_a_search_that_matches_nothing_is_an_empty_table_not_an_error(self):
        """A search matching nothing still carries `_search_rank`.

        The table layer orders by it, and Django resolves an ordering name
        against the model whether or not a row is built, so a bare `.none()`
        would answer 500 on every list page to a misspelt vendor."""

        Project.objects.create(name="Something else entirely")

        for path in ("/projects/", "/expenses/", "/docs/", "/audit/", "/receipts/"):
            with self.subTest(path=path):
                response = self.client.get(path, {"q": "zzzznomatchhere"})
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Something else entirely")


class NavigationTests(SignedInTestCase):
    """The bar has to say where you are, and only where you are."""

    def _entries(self, url):
        from hq.platform.core.context_processors import nav

        response = self.client.get(url)
        request = response.wsgi_request
        return nav(request)["nav_entries"]

    def test_every_entry_lights_its_own_group_and_no_other(self):
        """Checked for every entry the registry declares, so a new one cannot
        claim someone else's section: an entry with no namespace would
        light its group on every root page, the dashboard included."""

        from django.test import RequestFactory
        from django.urls import NoReverseMatch, resolve

        from hq.platform.application.domains import domain_navigation
        from hq.platform.core.context_processors import nav

        for entry in domain_navigation():
            try:
                url = reverse(entry.route)
            except NoReverseMatch:
                continue
            with self.subTest(route=entry.route):
                request = RequestFactory().get(url)
                request.resolver_match = resolve(url)
                request.user = self.user
                lit = {
                    group["label"]
                    for group in nav(request)["nav_entries"]
                    if group["kind"] == "group" and group["active"]
                }
                self.assertEqual(lit, {entry.group} if entry.group else set())

    def test_only_the_current_page_is_marked_active(self):
        # Every entry in a section shares one namespace, so matching on that would
        # light the whole dropdown at once.
        entries = self._entries(reverse("expenses:list"))
        active = [item["label"] for entry in entries for item in (entry.get("items") or [entry]) if item.get("active")]
        self.assertEqual(active, ["Expenses"])

    def test_the_section_holding_the_page_is_marked_even_with_no_entry(self):
        # The create page has no nav entry; its section must still show. Which
        # group that is comes from the registry rather than being restated here,
        # so regrouping a section stays a one-line change in one file.
        from hq.platform.application.domains import domain_navigation

        holder = next(item.group for item in domain_navigation() if item.route == "expenses:list")
        entries = self._entries(reverse("expenses:create"))
        groups = {entry["label"]: entry for entry in entries if entry["kind"] == "group"}
        self.assertTrue(groups[holder]["active"])
        self.assertEqual([label for label, entry in groups.items() if entry["active"]], [holder])

    def test_system_sorts_after_every_section_that_holds_work(self):
        entries = self._entries(reverse("dashboard"))
        labels = [entry["label"] for entry in entries]
        self.assertEqual(labels[-1], "System")
