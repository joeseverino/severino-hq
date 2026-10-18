"""What a page costs over the bench estate: the same at any size, and nothing derived twice."""

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
from hq.platform.application.derivation_testing import frozen
from hq.platform.application.derivations import counting, uncached
from hq.platform.application.projection import projection_scope, years_of
from hq.platform.application.services import service_catalog
from hq.platform.application.tests.host_only import host_only
from hq.platform.core.bench import Seeded, seed
from hq.platform.core.management.commands.bench_pages import SAMPLES, _pages

from .estate_sizes import SMALL, at_both_sizes


def _queries(run) -> list[str]:
    with host_only(), CaptureQueriesContext(connection) as captured:
        run()
    return [query["sql"] for query in captured.captured_queries]


def _catalog() -> None:
    with projection_scope(), uncached():
        service_catalog()


class ServiceCatalogBudgetTests(TestCase):
    def test_the_catalog_costs_the_same_at_any_size(self):
        small, large = at_both_sizes(lambda _seeded: len(_queries(_catalog)))

        self.assertEqual(large, small)

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
        year = timezone.localdate().year
        summaries = []

        def cost(_seeded: Seeded) -> int:
            with CaptureQueriesContext(connection) as captured:
                summaries.append(year_summary(year))
            return len(captured)

        small, large = at_both_sizes(cost)

        self.assertEqual(large, small)
        summary = summaries[-1]
        linked = [row for row in summary["documentation"] if row["related_projects"]]
        self.assertTrue(linked)
        self.assertGreater(len(summary["content"]), 10)


class YearsOfTests(TestCase):
    def test_it_answers_as_dates_does_at_no_more_cost(self):
        seed(SMALL)
        Asset.objects.filter(pk=Asset.objects.order_by("pk")[0].pk).update(purchase_date=None)

        for model, field in ((Expense, "date"), (Asset, "purchase_date")):
            with CaptureQueriesContext(connection) as asked:
                found = years_of(model, field)
            with CaptureQueriesContext(connection) as dates:
                expected = [day.year for day in model.objects.dates(field, "year")]
            self.assertEqual(found, expected)
            self.assertLessEqual(len(asked), len(dates))


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


# The pages measured once their derivations are stored.
STORED_PAGES = (
    "action_item_count",
    "dashboard",
    "action_items",
    "control_plane:findings",
    "control_plane:topology",
    "control_plane:tailnet",
    "control_plane:machine",
    "control_plane:detail",
    "control_plane:services",
    "control_plane:service",
    "zones:detail",
    "calendar:month",
    "search?q=example",
)
# The topology and its findings are derived per reader: the queue's and the operator's.
READERS = 2


class StoredPageBudgetTests(TestCase):
    """A page between two changes of the estate derives nothing, at any size."""

    def _costs(self, seeded: Seeded) -> tuple[dict[str, tuple[int, int]], Counter, Counter]:
        self.client.force_login(seeded.user)
        urls = dict(_pages(seeded)[0])
        with host_only(), frozen():
            with counting() as (first, _served):
                for name in STORED_PAGES:
                    self.assertEqual(self.client.get(urls[name]).status_code, 200, name)
            found, again = {}, Counter()
            for name in STORED_PAGES:
                with counting() as (ran, served), CaptureQueriesContext(connection) as captured:
                    self.client.get(urls[name])
                again.update(ran)
                found[name] = (len(captured), sum(served.values()))
        return found, Counter(first), again

    def test_a_page_costs_the_same_at_any_size_and_a_second_visit_derives_nothing(self):
        (small, small_first, small_again), (large, large_first, large_again) = at_both_sizes(self._costs)

        self.assertEqual(large, small)
        self.assertEqual(large_first, small_first)
        self.assertLessEqual(max(large_first.values()), READERS)
        self.assertEqual((small_again, large_again), (Counter(), Counter()))


class BenchPagesTests(TestCase):
    def test_every_sample_names_a_route_and_a_seeded_record(self):
        seeded = seed(SMALL)

        pages, left = _pages(seeded)

        benched = {name.split("?")[0] for name, _ in pages}
        self.assertLessEqual(set(SAMPLES), benched)
        self.assertNotIn("dashboard", dict(left))
