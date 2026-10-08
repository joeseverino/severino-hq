"""What a page costs over the bench estate, pinned so a per-row read cannot return."""

from collections import Counter
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
from hq.platform.application.derivations import counting, uncached
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
    with projection_scope(), uncached():
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


# What a page costs once its derivations are stored: (queries, derivations served).
# The same at both sizes, and no derivation runs.
STORED_PAGE_COSTS = {
    # The session, the person, the table revisions and one stored number.
    "action_item_count": (4, 1),
    "dashboard": (31, 9),
    "action_items": (7, 1),
    "control_plane:findings": (6, 1),
    "control_plane:topology": (6, 1),
    "control_plane:tailnet": (12, 2),
    # A page about one thing is also served the open problems about it.
    "control_plane:machine": (21, 3),
    "control_plane:detail": (19, 2),
    "control_plane:services": (14, 1),
    "control_plane:service": (19, 3),
    "zones:detail": (22, 3),
    "calendar:month": (12, 6),
    "search?q=example": (22, 1),
}
# The derivations one change of the estate costs, whichever page pays for them.
# The topology and its findings are derived per reader: the queue's and the operator's.
DERIVATIONS_PER_CHANGE = {
    "attention.by_subject": 1,
    "attention.count": 1,
    "attention.queue": 1,
    "attention.work": 1,
    "calendar.certificates": 1,
    "calendar.changes": 1,
    "calendar.containers": 1,
    "calendar.deploys": 1,
    "calendar.mine": 1,
    "calendar.registrations": 1,
    "dashboard.highlights": 1,
    "dashboard.links": 1,
    "dashboard.sections": 1,
    "dashboard.snapshot": 1,
    "estate.findings": 2,
    "estate.relations": 1,
    "estate.services": 1,
    "estate.topology": 2,
}


class StoredPageBudgetTests(TestCase):
    """A page between two changes of the estate derives nothing."""

    def _costs(self, scale: float) -> tuple[dict[str, tuple[int, int]], Counter, Counter]:
        seeded = seed(scale)
        self.client.force_login(seeded.user)
        urls = dict(_pages(seeded)[0])
        with (
            mock.patch("hq.platform.application.domains.extension_domains", return_value=()),
            mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()),
        ):
            with counting() as (first, _served):
                for name in STORED_PAGE_COSTS:
                    self.assertEqual(self.client.get(urls[name]).status_code, 200, name)
            found, again = {}, Counter()
            for name in STORED_PAGE_COSTS:
                with counting() as (ran, served), CaptureQueriesContext(connection) as captured:
                    self.client.get(urls[name])
                again.update(ran)
                found[name] = (len(captured), sum(served.values()))
        return found, first, again

    def test_a_small_estate_costs_the_pinned_queries_and_derives_nothing(self):
        found, first, again = self._costs(SMALL)

        self.assertEqual(found, STORED_PAGE_COSTS)
        self.assertEqual(dict(first), DERIVATIONS_PER_CHANGE)
        self.assertEqual(again, Counter())

    def test_a_large_estate_costs_the_same(self):
        found, first, again = self._costs(LARGE)

        self.assertEqual(found, STORED_PAGE_COSTS)
        self.assertEqual(dict(first), DERIVATIONS_PER_CHANGE)
        self.assertEqual(again, Counter())


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
