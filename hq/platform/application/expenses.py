"""Expense commands shared by web, MCP, and CLI."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from django.db.models import Sum

from hq.domains.expenses.models import DEFAULT_CATEGORY, Expense

from .command_fields import Describe, Names, Refers
from .money import money
from .projection import iso
from .record_saves import Held, Link, linked_saver
from .sensitivity import SAFE_SENSITIVITIES
from .ui import Kpi


@dataclass(frozen=True, slots=True)
class CostTotals:
    """What a list of costs adds up to, over every row its filters leave."""

    total: Decimal
    deductible: Decimal
    # A search or a filter is on, so the sums are of what matches.
    narrowed: bool = False

    def __bool__(self) -> bool:
        return bool(self.total or self.deductible)


def cost_totals(sums: dict[str, Any], *, narrowed: bool) -> CostTotals:
    """``sums`` is an aggregate's ``total`` and ``deductible``, either None over no rows."""

    zero = Decimal("0.00")
    return CostTotals(sums.get("total") or zero, sums.get("deductible") or zero, narrowed)


def costs_by_category(rows, *, query, choices, narrowed: bool) -> tuple[CostTotals, tuple[Kpi, ...]]:
    """What a list of expenses adds up to, whole and by category, from one statement.

    ``rows`` is the list as its filters leave it and ``query`` the request's
    own, which each category's link keeps. Each category is a figure linking
    to the list narrowed to it, said only where the categories divide the
    list: two or more, and none already chosen.
    """

    sums = list(
        Expense.objects.filter(pk__in=rows.order_by().values("pk"))
        .order_by()
        .values("category")
        .annotate(total=Sum("total_cost"), deductible=Sum("estimated_deductible_amount"))
    )
    zero = Decimal("0.00")
    totals = CostTotals(
        sum((row["total"] or zero for row in sums), zero),
        sum((row["deductible"] or zero for row in sums), zero),
        narrowed,
    )
    if len(sums) < 2 or query.get("category"):
        return totals, ()
    labels = dict(choices)
    kept = query.copy()
    kept.pop("page", None)

    def narrowed_to(category: str) -> str:
        kept["category"] = category
        return f"?{kept.urlencode()}"

    return totals, tuple(
        Kpi(
            labels.get(row["category"], row["category"]),
            money(row["total"] or zero, cents=False),
            url=narrowed_to(row["category"]),
        )
        for row in sorted(sums, key=lambda row: row["total"] or zero, reverse=True)
    )


@dataclass(frozen=True, slots=True)
class ExpenseCommand:
    date: Annotated[date, Describe("When it was spent.")]
    vendor: Annotated[str, Describe("Who was paid.")]
    item: Annotated[str, Describe("What was bought.")]
    category: Annotated[str, Describe("What type of expense it is.")] = DEFAULT_CATEGORY
    total_cost: Annotated[Decimal, Describe("What it cost in all.")] = Decimal("0.00")
    business_use_percentage: Annotated[int, Describe("How much of it is for the business, as a percentage.")] = 100
    payment_method: Annotated[str, Describe("How it was paid.")] = ""
    business_purpose: Annotated[str, Describe("Why the business needed it.")] = ""
    notes: Annotated[str, Describe("Anything else worth keeping.")] = ""
    related_project: Annotated[str | None, Names("projects"), Describe("The project this was spent on.")] = None
    related_asset: Annotated[str | None, Names("assets"), Describe("The asset this bought or kept running.")] = None
    related_content: Annotated[
        str | None, Names("content", key="slug"), Describe("The writeup or page this paid for.")
    ] = None
    related_documentation: Annotated[
        str | None, Names("documentation", key="doc_id"), Describe("The doc that records this expense.")
    ] = None
    paid_from: Annotated[str, Refers(Expense, "paid_from"), Describe("The account that paid, as type:identity.")] = ""
    about: Annotated[str, Refers(Expense, "about"), Describe("What it was for, as type:identity.")] = ""


def serialize_expense(expense: Expense) -> dict[str, Any]:
    doc = expense.related_documentation
    return {
        "id": expense.id,
        "date": iso(expense.date),
        "vendor": expense.vendor,
        "item": expense.item,
        "category": expense.category,
        "total_cost": str(expense.total_cost),
        "business_use_percentage": expense.business_use_percentage,
        "estimated_deductible_amount": str(expense.estimated_deductible_amount),
        "payment_method": expense.payment_method,
        "business_purpose": expense.business_purpose,
        "notes": expense.notes,
        "related_project": expense.related_project.slug if expense.related_project else None,
        "related_asset": expense.related_asset.slug if expense.related_asset else None,
        "related_content": expense.related_content.slug if expense.related_content else None,
        "related_documentation": (doc.doc_id if doc and doc.sensitivity in SAFE_SENSITIVITIES else None),
        "paid_from": expense.paid_from,
        "about": expense.about,
        "updated_at": iso(expense.updated_at),
    }


save_expense = linked_saver(
    "expenses",
    serialize_expense,
    refusals=Held("That expense was not found.", "That expense changed after it was read."),
    links={
        "related_project": Link("slug", "A related project was not found."),
        "related_asset": Link("slug", "A related asset was not found."),
        "related_content": Link("slug", "A related writeup or page was not found."),
        "related_documentation": Link("doc_id", "A related documentation record was not found."),
    },
)
