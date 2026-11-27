"""What a page costs over the bench estate: the same at any size, and nothing derived twice."""

from collections import Counter

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application.derivation_testing import frozen
from hq.platform.application.derivations import counting
from hq.platform.application.tests.host_only import host_only
from hq.platform.core.bench import Seeded
from hq.platform.core.management.commands.bench_pages import _pages

from .estate_sizes import at_both_sizes

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
    "jobs:list",
    "command:run",
    "control_plane:services",
    "control_plane:service",
    "control_plane:connections",
    "control_plane:events",
    "control_plane:containers",
    "control_plane:machines",
    "hq_self",
    "zones:detail",
    "calendar:month",
    "search?q=example",
)
# The topology and its findings are derived per reader: the queue's and the operator's.
READERS = 2


class StoredPageBudgetTests(TestCase):
    """A page between two changes of the estate derives nothing, at any size.

    The estate adopts the example procedures, so it holds automation runs in
    every state at both sizes, which Jobs, the machine's page and the
    procedure's command list.
    """

    def _costs(self, seeded: Seeded) -> tuple[dict[str, tuple[int, int]], Counter, Counter, dict[str, list[str]]]:
        self.client.force_login(seeded.user)
        urls = dict(_pages(seeded)[0])
        with host_only(), frozen():
            with counting() as (first, _served):
                for name in STORED_PAGES:
                    self.assertEqual(self.client.get(urls[name]).status_code, 200, name)
            found, again, repeated = {}, Counter(), {}
            for name in STORED_PAGES:
                with counting() as (ran, served), CaptureQueriesContext(connection) as captured:
                    self.client.get(urls[name])
                again.update(ran)
                found[name] = (len(captured), sum(served.values()))
                asked = Counter(query["sql"] for query in captured.captured_queries)
                if twice := [sql for sql, times in asked.items() if times > 1]:
                    repeated[name] = twice
        return found, Counter(first), again, repeated

    def test_a_page_costs_the_same_at_any_size_asks_each_query_once_and_a_second_visit_derives_nothing(self):
        (small, small_first, small_again, small_twice), (large, large_first, large_again, large_twice) = at_both_sizes(
            self._costs
        )

        self.assertEqual(large, small)
        self.assertEqual(large_first, small_first)
        self.assertLessEqual(max(large_first.values()), READERS)
        self.assertEqual((small_again, large_again), (Counter(), Counter()))
        self.assertEqual((small_twice, large_twice), ({}, {}))
