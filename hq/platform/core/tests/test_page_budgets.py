"""What a page costs over the bench estate, pinned so a per-row read cannot return."""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse as django_reverse
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.control_plane.provider_spec import adapter
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.expenses.models import Expense
from hq.domains.reports.exports import year_summary
from hq.platform.application import routes
from hq.platform.application.projection import projection_scope, years_of
from hq.platform.application.services import service_catalog
from hq.platform.core.bench import seed
from hq.platform.core.management.commands.bench_pages import SAMPLES, _pages

# Two sizes of the same estate: a cost that grows with rows differs between them.
SMALL, LARGE = 0.05, 0.25
# The year summary: its aggregates, and one read per prefetched relation.
YEAR_SUMMARY_QUERIES = 18
# The service catalogue: every reading it joins, each taken once.
SERVICE_CATALOG_QUERIES = 7


def _queries(run) -> list[str]:
    with (
        mock.patch("hq.platform.application.domains.extension_domains", return_value=()),
        mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()),
        CaptureQueriesContext(connection) as captured,
    ):
        run()
    return [query["sql"] for query in captured.captured_queries]


def _catalog() -> None:
    with projection_scope():
        service_catalog()


class ServiceCatalogBudgetTests(TestCase):
    def test_a_small_estate_costs_the_pinned_queries(self):
        seed(SMALL)

        self.assertEqual(len(_queries(_catalog)), SERVICE_CATALOG_QUERIES)

    def test_a_large_estate_costs_the_same(self):
        seed(LARGE)

        self.assertEqual(len(_queries(_catalog)), SERVICE_CATALOG_QUERIES)

    def test_a_service_still_names_the_container_found_running_it(self):
        seed(SMALL)
        with projection_scope():
            running = [
                facet.observed
                for service in service_catalog()
                for facet in service.facets
                if facet.observed is not None
            ]

        self.assertTrue(running)
        self.assertTrue(all(found.name.startswith("app") for found in running))


class YearSummaryBudgetTests(TestCase):
    def test_related_records_come_from_the_prefetch_at_any_size(self):
        seed(LARGE)
        year = timezone.localdate().year

        with self.assertNumQueries(YEAR_SUMMARY_QUERIES):
            summary = year_summary(year)

        linked = [row for row in summary["documentation"] if row["related_projects"]]
        self.assertTrue(linked)
        self.assertGreater(len(summary["content"]), 10)


class YearsOfTests(TestCase):
    def test_it_answers_as_dates_does_in_one_query(self):
        seed(SMALL)
        Asset.objects.filter(pk=Asset.objects.order_by("pk")[0].pk).update(purchase_date=None)

        for model, field in ((Expense, "date"), (Asset, "purchase_date")):
            with self.assertNumQueries(1):
                found = years_of(model, field)
            self.assertEqual(found, [day.year for day in model.objects.dates(field, "year")])


class SpecAdapterTests(SimpleTestCase):
    def test_a_spec_type_is_given_one_validator(self):
        for provider in PROVIDERS.values():
            self.assertIs(adapter(provider.spec_type), adapter(provider.spec_type))


class RouteContextTests(SimpleTestCase):
    def test_a_projection_asks_django_where_it_is_served_once(self):
        with (
            mock.patch.object(routes, "get_urlconf", wraps=routes.get_urlconf) as urlconf,
            mock.patch.object(routes, "get_script_prefix", wraps=routes.get_script_prefix) as prefix,
            projection_scope(),
        ):
            found = [routes.reverse("expenses:detail", args=[pk]) for pk in range(1, 40)]

        self.assertEqual(found, [django_reverse("expenses:detail", args=[pk]) for pk in range(1, 40)])
        self.assertEqual((urlconf.call_count, prefix.call_count), (1, 1))

    def test_outside_a_projection_each_lookup_asks(self):
        with mock.patch.object(routes, "get_urlconf", wraps=routes.get_urlconf) as urlconf:
            routes.reverse("dashboard")
            routes.reverse("dashboard")

        self.assertEqual(urlconf.call_count, 2)


class BenchPagesTests(TestCase):
    def test_every_sample_names_a_route_and_a_seeded_record(self):
        seeded = seed(SMALL)

        pages, left = _pages(seeded)

        benched = {name.split("?")[0] for name, _ in pages}
        self.assertLessEqual(set(SAMPLES), benched)
        self.assertNotIn("dashboard", dict(left))

    def test_the_dashboard_answers_over_the_seeded_estate(self):
        seeded = seed(SMALL)
        self.client.force_login(seeded.user)

        self.assertEqual(self.client.get(django_reverse("dashboard")).status_code, 200)
