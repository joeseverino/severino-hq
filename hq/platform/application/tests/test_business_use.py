"""The business-use range is one rule, declared on the model and held on every path.

Each door's command takes it as ``business_use.BusinessUseShare``, whose
bounds test_record_bounds holds to the model's; the form's refusal is
test_forms'."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from hq.domains.assets.models import Asset
from hq.domains.expenses.models import Expense

from ..money import quantize_money

IN_RANGE = (0, 100)


class RuleTests(TestCase):
    def test_the_bounds_are_inclusive(self):
        for value in IN_RANGE:
            with self.subTest(value=value):
                Asset(item_name="Example asset", business_use_percentage=value).full_clean()

    def test_out_of_range_names_the_bound_it_broke(self):
        for value, code in ((150, "max_value"), (-1, "min_value")):
            with self.subTest(value=value), self.assertRaises(ValidationError) as caught:
                Asset(item_name="Example asset", business_use_percentage=value).full_clean()
            (error,) = caught.exception.error_dict["business_use_percentage"]
            self.assertEqual(error.code, code)

    def test_the_table_refuses_a_row_written_around_validation(self):
        for create in (
            lambda: Asset.objects.create(item_name="Example asset", business_use_percentage=150),
            lambda: Expense.objects.create(date=date(2026, 7, 25), vendor="V", item="I", business_use_percentage=150),
        ):
            with self.subTest(create=create), self.assertRaises(IntegrityError), transaction.atomic():
                create()


EDGES = (
    ("0.00", 50),
    ("123.45", 0),
    ("12.35", 100),
    ("0.02", 75),  # 0.015: a double holds it just under half a cent
    ("0.03", 50),
    ("1.01", 50),
    ("12.35", 30),
    ("0.01", 49),  # 0.0049
    ("0.01", 50),  # 0.005
    ("9.99", 1),  # 0.0999
    ("9999999999.99", 50),
    ("9999999999.99", 99),
    ("9999999999.99", 100),
    ("-0.03", 50),  # a refund rounds away from zero
    ("-9999999999.99", 33),
)


def _sweep() -> list[tuple[Decimal, int]]:
    edges = [(Decimal(cost), pct) for cost, pct in EDGES]
    every_cent = [(Decimal(cents) / 100, pct) for cents in range(100) for pct in range(101)]
    return edges + every_cent


class DeductibleTests(TestCase):
    """The table derives the deductible amount, rounded as ``quantize_money`` rounds, on every write path."""

    def _assert_matches(self, model, rows: list[tuple[Decimal, int]]) -> None:
        stored = model.objects.order_by("pk").values_list(
            "total_cost", "business_use_percentage", "estimated_deductible_amount"
        )
        wrong = [
            (cost, pct, amount)
            for cost, pct, amount in stored
            if amount != quantize_money(cost * Decimal(pct) / Decimal(100))
        ]
        self.assertEqual(wrong, [])
        self.assertEqual(len(stored), len(rows))

    def test_a_bulk_insert_rounds_half_up_like_quantize_money(self):
        rows = _sweep()
        Asset.objects.bulk_create(
            Asset(item_name="Example asset", slug=f"example-{n}", total_cost=cost, business_use_percentage=pct)
            for n, (cost, pct) in enumerate(rows)
        )
        Expense.objects.bulk_create(
            Expense(date=date(2026, 7, 25), vendor="V", item="I", total_cost=cost, business_use_percentage=pct)
            for cost, pct in rows
        )
        self._assert_matches(Asset, rows)
        self._assert_matches(Expense, rows)

    def test_an_update_keeps_it_current(self):
        expense = Expense.objects.create(
            date=date(2026, 7, 25), vendor="V", item="I", total_cost=Decimal("100.00"), business_use_percentage=75
        )
        self.assertEqual(expense.estimated_deductible_amount, Decimal("75.00"))
        Expense.objects.filter(pk=expense.pk).update(total_cost=Decimal("0.02"))
        expense.refresh_from_db()
        self.assertEqual(expense.estimated_deductible_amount, Decimal("0.02"))
        expense.business_use_percentage = 40
        expense.save()
        self.assertEqual(expense.estimated_deductible_amount, Decimal("0.01"))
