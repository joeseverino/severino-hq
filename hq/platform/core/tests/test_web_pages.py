"""The web shell's pages: search, lists, the dashboard and the navigation."""

import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
)
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project
from hq.platform.application import readings
from hq.platform.application.action_links import command_url
from hq.platform.application.dashboard import operating_snapshot
from hq.platform.application.labels import DOT
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.host_only import host_only
from hq.platform.application.tests.markup import elements, hrefs, select, text
from hq.platform.application.tests.writers import form_keyed

from .signed_in_case import _AuthedTestCase


class SearchPageTests(_AuthedTestCase):
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

        self.assertTemplateUsed(response, "core/_command_center_results.html")
        self.assertTrue(select(response, "data-command-center-results"))
        self.assertFalse(elements(response, "html"))
        records.assert_called_once()
        self.assertEqual(records.call_args.args, ("certificate",))
        self.assertEqual(records.call_args.kwargs["limit_per_scope"], 3)
        contacts.assert_not_called()

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
        shown = {each.attrs["id"] for each in select(response, "id") if each.attrs["id"].startswith("scope-")}
        self.assertEqual(shown, {f"scope-{group['scope']}" for group in response.context["groups"] if group["count"]})


class EmptyListTests(_AuthedTestCase):
    def test_an_empty_list_is_the_empty_state_and_no_table(self):
        """Headings over no rows say nothing; the one empty state says what is missing."""

        page = self.client.get(reverse("expenses:list"))

        self.assertTrue(select(page, "data-empty-state"))
        self.assertFalse(elements(page, "table"))

    def test_a_list_emptied_by_its_filter_offers_the_way_back(self):
        page = self.client.get(reverse("expenses:list"), {"q": "nothing-matches-this"})

        self.assertTrue(select(page, "data-empty-state"))
        self.assertIn(page.context["table"]["clear"].url, hrefs(page))
        self.assertFalse(elements(page, "table"))


class KeyColumnTests(_AuthedTestCase):
    """A list says which columns a phone keeps, on the heading, sortable or not."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # An empty list draws no table, so its headings need rows under them.
        from hq.platform.core import example_estate
        from hq.platform.core.example_estate.testing import throwaway_media

        with throwaway_media():
            example_estate.seed()

    def test_a_sortable_heading_carries_its_columns_classes(self):
        page = self.client.get(reverse("expenses:list"))

        headings = elements(page, "th")

        def heading(label):
            return next(each.attrs for each in headings if each.text.strip().startswith(label))

        # Cost sorts, and is one of the two an expense is read for.
        self.assertIn("aria-sort", heading("Cost"))
        self.assertIn("key-col", heading("Cost").get("class", "").split())
        # A column that is not key is not kept on a phone.
        self.assertIn("aria-sort", heading("Category"))
        self.assertNotIn("key-col", heading("Category").get("class", "").split())

    def test_every_shared_list_names_at_least_one_key_column(self):
        """With none, a phone would show only the column that names the row."""

        for name in ("expenses:list", "projects:list", "content:list", "assets:list", "core:audit_list"):
            with self.subTest(page=name):
                headings = elements(self.client.get(reverse(name)), "th")
                self.assertTrue(any("key-col" in heading.attrs.get("class", "").split() for heading in headings))


class DashboardWorkflowTests(_AuthedTestCase):
    def test_dashboard_and_machine_page_share_the_machine_observation(self):
        observed = timezone.now()
        machine = ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={"name": "example-host"},
        )
        readings.record(
            readings.machine_telemetry("example-host"),
            {
                "status": "good",
                "summary": "Host load 0.30",
                "metrics": [{"label": "CPU", "value": "9%", "detail": "8 cores"}],
            },
            observed_at=observed,
        )
        DashboardMachine.objects.create(machine=machine)

        dashboard = self.client.get("/")
        machine_page = self.client.get("/infrastructure/machines/example-host/")

        self.assertIn("9%", text(dashboard))
        self.assertIn("9%", text(machine_page))

    def test_dashboard_refresh_is_one_explicit_controller_request(self):
        machine = ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={"name": "example-host"},
        )
        DashboardMachine.objects.create(machine=machine)
        ProviderConnection.objects.create(
            connection_ref="example-portainer",
            controller_id="example-host",
            provider="portainer",
            endpoint="https://portainer.example.test",
            reaches=["example-host"],
            reachable=True,
            observed_at=timezone.now(),
        )

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("hq.platform.application.glance.ring_glance_doorbell") as ring,
        ):
            response = self.client.post("/dashboard/glance/", form_keyed())

        self.assertEqual(response.status_code, 202)
        self.assertTrue(select(response, "data-refreshing"))
        self.assertEqual(
            list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)),
            [f"machine-{machine.pk}"],
        )
        ring.assert_called_once_with()

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
        ContentItem.objects.create(title="Count me", status=ContentItem.Status.DRAFT)

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

        marker = Path(tempfile.mkdtemp()) / "controller-heartbeat"
        with self.settings(SEVERINO_CONTROLLER_HEARTBEAT=str(marker)):
            never = self.client.get("/")
            marker.touch()
            arriving = self.client.get("/")
            long_ago = time.time() - 3600
            os.utime(marker, (long_ago, long_ago))
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
        # It opens the panel, and is a plain link to it without script.
        # A link's name holds the summary's parts; the dots between them are drawn, never read.
        badge = next(
            each
            for each in select(page, "data-modal-open", "connection")
            if all(part in each.text for part in found.summary.split(DOT))
        )
        self.assertEqual(badge.attrs["href"], reverse("connection"))
        self.assertIn(found.transport, badge.text)

    def test_dashboard_routes_infrastructure_findings_to_their_evidence(self):
        ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec={},
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

    def test_dashboard_excludes_site_pages_from_docs_review(self):
        DocumentationRecord.objects.create(
            doc_id="page-about",
            title="About page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
        )

        self.assertEqual(operating_snapshot(principal=cli_principal())["kpis"]["docs_needing_review"], 0)

    def test_projects_can_filter_for_missing_output(self):
        project = Project.objects.create(name="No output yet", status=Project.Status.ACTIVE)
        documented = Project.objects.create(name="Documented", status=Project.Status.ACTIVE)
        doc = DocumentationRecord.objects.create(
            doc_id="rb-documented-001",
            title="Documented runbook",
            status=DocumentationRecord.Status.ACTIVE,
        )
        doc.related_projects.add(documented)
        content = ContentItem.objects.create(title="Documented post")
        content.related_projects.add(documented)

        response = self.client.get("/projects/?needs_output=1")

        self.assertEqual(list(response.context["object_list"]), [project])

    def test_archived_projects_sort_last_by_default(self):
        active = Project.objects.create(name="ZZZ active project", status=Project.Status.ACTIVE)
        archived = Project.objects.create(name="AAA archived project", status=Project.Status.ARCHIVED)

        response = self.client.get("/projects/")

        listed = list(response.context["object_list"])
        self.assertLess(listed.index(active), listed.index(archived))

    def test_table_filters_accept_multiple_values_and_compose(self):
        active_lab = Project.objects.create(
            name="Active lab",
            status=Project.Status.ACTIVE,
            category="homelab",
        )
        idea_lab = Project.objects.create(
            name="Idea lab",
            status=Project.Status.IDEA,
            category="homelab",
        )
        Project.objects.create(
            name="Archived lab",
            status=Project.Status.ARCHIVED,
            category="homelab",
        )
        Project.objects.create(
            name="Active consulting",
            status=Project.Status.ACTIVE,
            category="other",
        )

        response = self.client.get(
            "/projects/",
            {"status": ["active", "idea"], "category": ["homelab"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(response.context["object_list"], [active_lab, idea_lab])
        status_filter = next(item for item in response.context["table"]["filters"] if item["name"] == "status")
        self.assertEqual(status_filter["selected_count"], 2)

    def test_table_sort_is_allowlisted(self):
        Project.objects.create(name="Safe sort")

        response = self.client.get("/projects/", {"sort": "not_a_model_field"})

        self.assertEqual(response.status_code, 200)

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

    def test_table_search_combines_terms_across_searchable_fields(self):
        matching = Project.objects.create(
            name="Operations console",
            technologies_used="Django SQLite",
        )
        Project.objects.create(name="Operations notes", technologies_used="Markdown")

        response = self.client.get("/projects/", {"q": "operations django"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["object_list"]), [matching])

    @override_settings(SEVERINO_DOC_REVIEW_INTERVAL_DAYS=30)
    def test_docs_review_filter_uses_configured_interval(self):
        current = DocumentationRecord.objects.create(
            doc_id="rb-current-001",
            title="Current runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=20),
        )
        stale = DocumentationRecord.objects.create(
            doc_id="rb-stale-001",
            title="Stale runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )
        site_page = DocumentationRecord.objects.create(
            doc_id="page-stale-001",
            title="Stale site page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )

        response = self.client.get("/docs/?needs_review=1")

        listed = list(response.context["object_list"])
        self.assertIn(stale, listed)
        self.assertNotIn(current, listed)
        self.assertNotIn(site_page, listed)

    @override_settings(SEVERINO_DOC_REVIEW_INTERVAL_DAYS=30)
    def test_reports_docs_review_uses_shared_definition(self):
        stale = DocumentationRecord.objects.create(
            doc_id="rb-stale-002",
            title="Stale runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )
        DocumentationRecord.objects.create(
            doc_id="page-stale-002",
            title="Stale site page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )

        response = self.client.get("/reports/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["docs_needing_review_count"], 1)
        self.assertEqual(
            [link.title for link, _reviewed in response.context["docs_needing_review"]],
            [stale.doc_id],
        )


class NavigationTests(TestCase):
    """The bar has to say where you are, and only where you are."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="nav")

    def setUp(self):
        self.client.force_login(self.user)

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
