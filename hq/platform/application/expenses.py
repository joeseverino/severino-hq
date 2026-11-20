"""Expense commands shared by web, MCP, and CLI."""

from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import Annotated, Any

from django.db.models import Count, QuerySet, Sum
from pydantic import Field

from hq.domains.expenses.models import DEFAULT_CATEGORY, EXPENSE_CATEGORY_CHOICES, Expense
lazy from hq.domains.expenses.specs import ExpenseRecord, ExpenseRow

from .assets import PaymentMethod
from .business_use import BusinessUseShare
from .card_items import Kpi
from .command_fields import Day, Describe, Names, Refers
from .entity_links import entity_link
from .money import money
from .projection import listing
from .record_errors import RecordNotFound
from .record_fields import Notes, TotalCost, one_of
from .record_saves import Held, Link, linked_saver, row
from .references import Kind
from .security import Principal
from .sensitivity import shared_doc
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


ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class CostTotals:
    """What a list of costs adds up to, over every row its filters leave."""

    total: Decimal = ZERO
    deductible: Decimal = ZERO
    count: int = 0
    # A search or a filter is on, so the sums are of what matches.
    narrowed: bool = False

    def __bool__(self) -> bool:
        return bool(self.total or self.deductible)


# Each sum in the database, in the whole cents each row already holds.
_SUMS = {"count": Count("pk"), "total": Sum("total_cost"), "deductible": Sum("estimated_deductible_amount")}


def _summed(sums: dict[str, Any], narrowed: bool = False) -> CostTotals:
    return CostTotals(sums["total"] or ZERO, sums["deductible"] or ZERO, sums["count"], narrowed)


def _each_once(rows: QuerySet) -> QuerySet:
    # A filter that joins (a search, a count of receipts) can repeat a row,
    # so the sums read each row of ``rows`` once.
    return rows.model.objects.filter(pk__in=rows.order_by().values("pk")).order_by()


def cost_totals(rows: QuerySet, *, narrowed: bool = False) -> CostTotals:
    """What ``rows``, expenses or assets as their filters leave them, add up to."""

    return _summed(_each_once(rows).aggregate(**_SUMS), narrowed)


def costs_by_category(rows: QuerySet) -> tuple[tuple[str, CostTotals], ...]:
    """What each category of ``rows`` (as ``cost_totals`` reads them) adds up to, the costliest first."""

    grouped = _each_once(rows).values("category").annotate(**_SUMS).order_by("-total", "category")
    return tuple((row["category"], _summed(row)) for row in grouped)


def category_figures(rows: QuerySet, *, query, choices) -> tuple[Kpi, ...]:
    """Each category's total as a figure linking to the list narrowed to it,
    said only where two or more divide the list, which a list narrowed to one
    category never is. ``query`` is the request's own, which each link keeps."""

    categories = costs_by_category(rows)
    if len(categories) < 2:
        return ()
    labels = dict(choices)
    kept = query.copy()
    kept.pop("page", None)

    def narrowed_to(category: str) -> str:
        kept["category"] = category
        return with_query("", kept)

    return tuple(
        Kpi(labels.get(category, category), money(totals.total, cents=False), url=narrowed_to(category))
        for category, totals in categories
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
    related_project: Annotated[ExpenseProject, Field(title="Project")] = None
    related_asset: Annotated[ExpenseAsset, Field(title="Asset")] = None
    related_content: Annotated[ExpenseContent, Field(title="Writeup or page")] = None
    related_documentation: Annotated[ExpenseDoc, Field(title="Document")] = None
    about: Annotated[ExpenseAbout, Field(title="Also for")] = ""


# What an expense's row reads of the records it is linked to.
_LINKED = ("related_project", "related_asset", "related_content", "related_documentation")


def list_expenses(
    *, year: int | None = None, category: str | None = None, limit: int = 50, principal: Principal | None = None
) -> dict[str, Any]:
    """List expense records whole, each doc as ``principal`` may read it."""
    rows = Expense.objects.select_related(*_LINKED)
    if year is not None:
        rows = rows.filter(date__year=year)
    if category:
        rows = rows.filter(category=category)
    return listing(rows, partial(serialize_expense, principal=principal), order=("-date", "-id"), limit=limit)


def serialize_expense(expense: Expense, *, principal: Principal | None = None) -> ExpenseRecord:
    """``expense`` whole, with the names its related records are said by, as
    ``principal`` may read it; None is an AI-facing surface's reader."""

    document = shared_doc(expense.related_documentation, principal)
    # The saved names of what ``paid_from`` and ``about`` refer to stay out: a
    # reference's owner says who may read its name.
    fields: ExpenseRow = row(expense, ExpenseRow, related_documentation=document.doc_id if document else None)
    return {
        **fields,
        "url": entity_link(Kind.EXPENSE, str(expense.pk)).url,
        "category_label": expense.get_category_display(),
        "payment_method_label": expense.get_payment_method_display() if expense.payment_method else "",
        "project_name": expense.related_project.name if expense.related_project else "",
        "asset_name": expense.related_asset.item_name if expense.related_asset else "",
        "content_title": expense.related_content.title if expense.related_content else "",
        "document_title": document.title if document else "",
    }


def get_expense(identifier: int, *, principal: Principal | None = None) -> ExpenseRecord:
    """One expense by its id."""

    expense = Expense.objects.select_related(*_LINKED).filter(pk=identifier).first()
    if expense is None:
        raise RecordNotFound("No expense has that id.")
    return serialize_expense(expense, principal=principal)


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
