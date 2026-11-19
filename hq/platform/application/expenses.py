"""Expense commands shared by web, MCP, and CLI."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

from django.db.models import Sum

from hq.domains.expenses.models import DEFAULT_CATEGORY, EXPENSE_CATEGORY_CHOICES, Expense
lazy from hq.domains.expenses.specs import ExpenseRow

from .assets import PaymentMethod
from .business_use import BusinessUseShare
from .card_items import Kpi
from .command_fields import Day, Describe, Names, Refers
from .money import money
from .projection import listing
from .record_fields import Notes, TotalCost, one_of
from .record_saves import Held, Link, linked_saver, row
from .url_query import with_query

# An expense's fields as its command writes them and its row reads them back, each said once.
_CATEGORY = one_of(EXPENSE_CATEGORY_CHOICES)
type ExpenseDate = Annotated[Day, Describe("When it was spent.")]
type ExpenseVendor = Annotated[str, Describe("Who was paid.")]
type ExpenseItem = Annotated[str, Describe("What was bought.")]
type ExpenseCategory = Annotated[_CATEGORY, Describe("What type of expense it is.")]
type BusinessPurpose = Annotated[str, Describe("Why the business needed it.", api="Empty when not recorded.")]
type ExpenseProject = Annotated[
    str | None, Names("projects"), Describe("The project this was spent on.", api="Its slug; null for none.")
]
type ExpenseAsset = Annotated[
    str | None,
    Names("assets"),
    Describe("The asset this bought or kept running.", api="Its slug; null for none."),
]
type ExpenseContent = Annotated[
    str | None,
    Names("content", key="slug"),
    Describe("The writeup or page this paid for.", api="Its slug; null for none."),
]
type ExpenseDoc = Annotated[
    str | None,
    Names("documentation", key="doc_id"),
    Describe(
        "The doc that records this expense.",
        api="Its vault doc ID; null for none, or for one too sensitive to share.",
    ),
]
type PaidFrom = Annotated[
    str,
    Refers(Expense, "paid_from"),
    Describe("The account that paid.", api="A `type:key` reference; empty when not recorded."),
]
type ExpenseAbout = Annotated[
    str,
    Refers(Expense, "about"),
    Describe(
        "What it was for, where none of the related records says it.",
        api="A `type:key` reference; empty for none.",
    ),
]


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
        return with_query("", kept)

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
    date: ExpenseDate
    vendor: ExpenseVendor
    item: ExpenseItem
    category: ExpenseCategory = DEFAULT_CATEGORY
    total_cost: TotalCost = Decimal("0.00")
    business_use_percentage: BusinessUseShare = 100
    payment_method: PaymentMethod = ""
    paid_from: PaidFrom = ""
    business_purpose: BusinessPurpose = ""
    notes: Notes = ""
    related_project: ExpenseProject = None
    related_asset: ExpenseAsset = None
    related_content: ExpenseContent = None
    related_documentation: ExpenseDoc = None
    about: ExpenseAbout = ""


def serialize_expense(expense: Expense) -> ExpenseRow:
    return row(expense, ExpenseRow)


def list_expenses(*, year: int | None = None, category: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List expense records with stable, sensitivity-safe relationships."""
    rows = Expense.objects.select_related(
        "related_project", "related_asset", "related_content", "related_documentation"
    )
    if year is not None:
        rows = rows.filter(date__year=year)
    if category:
        rows = rows.filter(category=category)
    return listing(rows, serialize_expense, order=("-date", "-id"), limit=limit)


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
