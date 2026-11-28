"""The expenses resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from django.db.models import Q

from hq.platform.application import expenses
from hq.platform.application.answer_fields import DeductibleAmount, PageUrl, TotalCostAmount, UpdatedAt
from hq.platform.application.assets import PaymentMethod
from hq.platform.application.business_use import BusinessUseShare
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import (
    DetailRead,
    ListFilter,
    ListRead,
    ListSort,
    ListToggle,
    ResourceSpec,
)
from hq.platform.application.projection import years_of
from hq.platform.application.record_fields import Notes, RecordId
from hq.platform.application.resources import BoundedQuery, SearchText
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import EXPENSE_CATEGORY_CHOICES, Expense


class ExpenseQuery(BoundedQuery):
    query: SearchText = None


# Each order the list is read in, the first where none is asked.
SORTS = (
    *ListSort.both("-date", "Newest expense", "Oldest expense"),
    *ListSort.both("vendor", "Vendor"),
    *ListSort.both("item", "Item"),
    *ListSort.both("category", "Category"),
    *ListSort.both("total_cost", "Lowest cost", "Highest cost"),
    *ListSort.both("business_use_percentage", "Lowest business use", "Highest business use"),
    *ListSort.both("estimated_deductible_amount", "Lowest deductible", "Highest deductible"),
)
FILTERS = (
    ListFilter("category", "Category", EXPENSE_CATEGORY_CHOICES),
    # The years the expenses are dated in, each a number.
    ListFilter(
        "year",
        "Year",
        lambda: [(year, str(year)) for year in years_of(Expense, "date")],
        lookup="date__year",
        pattern=r"^\d{1,4}(?:,\d{1,4})*$",
    ),
)


def _receiptless() -> tuple[tuple[int, ...], tuple[int, ...]]:
    """An expense with no receipt, kept, and one with its receipt, left out."""

    kept = Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting")
    proved = Expense.objects.create(date="2030-01-03", vendor="Example Host", item="Hosting")
    proved.receipts.create(vendor="Example Host")
    return (kept.pk,), (proved.pk,)


NO_RECEIPT = ListToggle(
    "no_receipts", "No receipt", "True for only the expenses with no receipt.", Q(receipts__isnull=True), _receiptless
)


class ExpenseRow(TypedDict):
    """One expense; each related record by its slug or id, or null."""

    id: Annotated[int, Describe("The expense's id, which its page and the records linked to it name it by.")]
    label: Annotated[str, Describe("What the expense is called wherever it is named: who was paid, and for what.")]
    date: expenses.ExpenseDate
    vendor: expenses.ExpenseVendor
    item: expenses.ExpenseItem
    category: expenses.ExpenseCategory
    total_cost: TotalCostAmount
    business_use_percentage: BusinessUseShare
    estimated_deductible_amount: DeductibleAmount
    payment_method: PaymentMethod
    business_purpose: expenses.BusinessPurpose
    notes: Notes
    related_project: expenses.ExpenseProject
    related_asset: expenses.ExpenseAsset
    related_content: expenses.ExpenseContent
    related_documentation: expenses.ExpenseDoc
    paid_from: expenses.PaidFrom
    about: expenses.ExpenseAbout
    updated_at: UpdatedAt


class ExpenseRecord(ExpenseRow):
    """One expense with the words and names its related records are said by."""

    url: PageUrl
    category_label: Annotated[str, Describe("Its category, in words.")]
    payment_method_label: Annotated[str, Describe("How it was paid for, in words; empty when not recorded.")]
    project_name: Annotated[str, Describe("The related project's name; empty for none.")]
    asset_name: Annotated[str, Describe("The related asset's name; empty for none.")]
    content_title: Annotated[str, Describe("The related writeup's or page's title; empty for none.")]
    document_title: Annotated[
        str, Describe("The related doc's title; empty for none, or for one too sensitive to share.")
    ]


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "expenses",
            "Expenses",
            "Expense records with stable relationship identifiers.",
            Capability.READ,
            listing=ListRead(
                expenses.list_expenses,
                ExpenseQuery,
                ExpenseRecord,
                sorts=SORTS,
                filters=FILTERS,
                toggles=(NO_RECEIPT,),
                totals=expenses.list_totals,
            ),
            detail=DetailRead(expenses.get_expense, ExpenseRecord),
            identifier="id",
            identifier_type=RecordId,
            pass_principal=True,
            search=SearchDefinition.of(
                Expense,
                "pk",
                ("category", "business_purpose", "notes"),
                timestamp_field="date",
            ),
            web_route="expenses:list",
        ),
    )
