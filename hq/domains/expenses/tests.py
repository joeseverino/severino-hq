"""Expenses and assets: what their lists add up to, and what their pages lead to."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense


class _Signed(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("ledger-reader"))


class ExpenseListTests(_Signed):
    def test_an_empty_list_offers_the_way_forward_and_no_empty_controls(self):
        response = self.client.get(reverse("expenses:list"))

        self.assertContains(response, 'No expenses yet. <a href="/expenses/new/">New expense</a>')
        self.assertNotContains(response, "Filtered")
        self.assertNotContains(response, "$0.00")
        # A year to filter by once an expense has one.
        self.assertNotIn("Year", [item["label"] for item in response.context["table"]["filters"]])

    def test_the_total_says_matching_only_when_something_narrows_the_list(self):
        Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("12.00"))
        Expense.objects.create(date="2030-02-02", vendor="Example Registrar", item="Domain", total_cost=Decimal("8.00"))

        whole = self.client.get(reverse("expenses:list"))
        narrowed = self.client.get(reverse("expenses:list"), {"category": "miscellaneous", "q": ""})

        self.assertContains(whole, "Total <strong>$20.00</strong>")
        self.assertNotContains(whole, "Matching:")
        self.assertContains(narrowed, "Matching: <strong>$20.00</strong> total")
        self.assertIn("Year", [item["label"] for item in whole.context["table"]["filters"]])

    def test_the_list_says_what_each_category_adds_up_to_as_a_link_to_it(self):
        Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting", category="hosting", total_cost=Decimal("12.00"))
        Expense.objects.create(date="2030-01-09", vendor="Example Host", item="Hosting", category="hosting", total_cost=Decimal("12.00"))
        Expense.objects.create(date="2030-02-02", vendor="Example Registrar", item="Domain", category="domains", total_cost=Decimal("8.00"))

        whole = self.client.get(reverse("expenses:list"), {"year": "2030", "page": "1"})
        chosen = self.client.get(reverse("expenses:list"), {"category": "hosting"})

        self.assertEqual(
            [(row.label, row.total, row.url) for row in whole.context["by_category"]],
            [
                ("Hosting", Decimal("24.00"), "?year=2030&category=hosting"),
                ("Domains", Decimal("8.00"), "?year=2030&category=domains"),
            ],
        )
        self.assertContains(whole, '<a class="chip" href="?year=2030&amp;category=hosting">Hosting <strong>$24.00</strong></a>')
        self.assertContains(whole, "Matching: <strong>$32.00</strong> total")
        # A list already narrowed to one category is not divided by category.
        self.assertEqual(chosen.context["by_category"], ())
        self.assertContains(chosen, "Matching: <strong>$24.00</strong> total")

    def test_one_category_is_not_a_division(self):
        Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("12.00"))

        response = self.client.get(reverse("expenses:list"))

        self.assertEqual(response.context["by_category"], ())
        self.assertNotContains(response, 'class="chip"')

    def test_the_totals_of_a_list_with_a_toggle_on_count_each_expense_once(self):
        from hq.domains.receipts.models import Receipt

        with_receipts = Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting", category="hosting", total_cost=Decimal("12.00"))
        Receipt.objects.create(vendor="Example Host", related_expense=with_receipts)
        Receipt.objects.create(vendor="Example Host", related_expense=with_receipts)
        Expense.objects.create(date="2030-02-02", vendor="Example Registrar", item="Domain", category="domains", total_cost=Decimal("8.00"))
        Expense.objects.create(date="2030-02-03", vendor="Example Host", item="Hosting", category="hosting", total_cost=Decimal("5.00"))

        response = self.client.get(reverse("expenses:list"), {"no_receipts": "1"})

        self.assertEqual(response.context["totals"].total, Decimal("13.00"))
        self.assertEqual(
            [(row.label, row.total) for row in response.context["by_category"]],
            [("Domains", Decimal("8.00")), ("Hosting", Decimal("5.00"))],
        )

    def test_the_totals_and_the_categories_are_one_statement(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting", category="hosting", total_cost=Decimal("12.00"))
        Expense.objects.create(date="2030-02-02", vendor="Example Registrar", item="Domain", category="domains", total_cost=Decimal("8.00"))

        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("expenses:list"))

        self.assertEqual(len([query for query in queries if "SUM(" in query["sql"]]), 1)

    def test_an_expense_names_its_document_by_title(self):
        record = DocumentationRecord.objects.create(doc_id="rb-example-renewal", title="Renew the example domain")
        expense = Expense.objects.create(
            date="2030-01-02", vendor="Example Registrar", item="Domain", related_documentation=record
        )

        response = self.client.get(expense.get_absolute_url())

        self.assertContains(response, 'title="rb-example-renewal">Renew the example domain</a>')
        self.assertNotContains(response, ">rb-example-renewal<")


class AssetPageTests(_Signed):
    def test_an_asset_with_no_cost_says_so_once(self):
        asset = Asset.objects.create(item_name="Example Vault", slug="example-vault")

        response = self.client.get(asset.get_absolute_url())

        self.assertContains(response, '<dt>Cost</dt><dd class="muted">Not recorded</dd>', html=True)
        self.assertNotContains(response, "$0.00")
        self.assertNotContains(response, "<dt>Slug</dt>")
        self.assertContains(response, "Nothing is linked to this asset yet.")

    def test_a_domain_asset_links_the_domain_hq_manages(self):
        from unittest import mock

        asset = Asset.objects.create(item_name="Example.com", slug="example-com", category="domain")
        other = Asset.objects.create(item_name="example.com", slug="example-server", category="server_hardware")

        with mock.patch("hq.platform.application.zones.zone_names", return_value=("example.com",)):
            response = self.client.get(asset.get_absolute_url())
            unrelated = self.client.get(other.get_absolute_url())

        self.assertContains(response, "<dt>Domain page</dt>")
        self.assertContains(response, f'href="{reverse("zones:detail", args=["example.com"])}"')
        self.assertNotContains(unrelated, "Domain page")

    def test_the_list_adds_up_what_it_shows(self):
        Asset.objects.create(item_name="Example Switch", slug="example-switch", total_cost=Decimal("100.00"))

        response = self.client.get(reverse("assets:list"))

        self.assertContains(response, "Total <strong>$100.00</strong>")
        self.assertContains(response, "Business use")
        self.assertNotContains(response, "% biz")
