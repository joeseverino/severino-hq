"""A list read does not fetch a relation once per row."""

from __future__ import annotations

from datetime import date

from django.core.exceptions import FieldFetchBlocked
from django.db.models import FETCH_RAISE
from django.test import TestCase, override_settings

from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project

from ..projection import guarded, listing
from ..tables import TableListMixin


def _expenses(count: int) -> None:
    for index in range(count):
        project = Project.objects.create(name=f"Example project {index}")
        Expense.objects.create(
            date=date(2026, 10, 1), vendor="Example", item=f"Item {index}", related_project=project
        )


class GuardedTests(TestCase):
    def setUp(self):
        _expenses(3)

    def test_under_test_an_unfetched_relation_raises(self):
        with self.assertRaisesRegex(FieldFetchBlocked, "Expense.related_project"):
            [expense.related_project for expense in guarded(Expense.objects.all())]

    def test_under_test_an_unfetched_deferred_field_raises(self):
        with self.assertRaisesRegex(FieldFetchBlocked, "Expense.vendor"):
            [expense.vendor for expense in guarded(Expense.objects.only("item"))]

    def test_a_relation_the_read_fetched_is_served(self):
        rows = guarded(Expense.objects.select_related("related_project"))

        self.assertEqual(len({expense.related_project.name for expense in rows}), 3)

    @override_settings(SEVERINO_STRICT_FETCH=False)
    def test_in_production_it_is_fetched_once_for_the_page(self):
        with self.assertNumQueries(2):
            names = {expense.related_project.name for expense in guarded(Expense.objects.all())}

        self.assertEqual(len(names), 3)

    def test_every_table_and_every_listing_is_guarded(self):
        class View(TableListMixin):
            class request:  # noqa: N801 - stands in for a request with no query
                GET = __import__("django.http").http.QueryDict("")

        rows = View().apply_table_query(Expense.objects.all())
        with self.assertRaises(FieldFetchBlocked):
            rows[0].related_project

        modes = listing(Project, lambda row: row._state.fetch_mode, search=())["items"]
        self.assertEqual(modes, [FETCH_RAISE] * 3)
