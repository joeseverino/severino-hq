"""The expenses resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field, create_model

from hq.domains.assets.specs import PaymentMethod
from hq.platform.application import read_models
from hq.platform.application.answer_fields import BusinessUseShare, DeductibleAmount, TotalCostAmount, UpdatedAt
from hq.platform.application.describe import Describe, listed
from hq.platform.application.expenses import ExpenseItem, ExpenseVendor
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.record_fields import Notes
from hq.platform.application.resources import BoundedQuery, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

from .models import EXPENSE_CATEGORY_CHOICES, Expense

ExpenseQuery = create_model(
    "ExpenseQuery",
    __base__=BoundedQuery,
    year=(int | None, Field(default=None, ge=1, le=9999, description="Only the expenses dated in this year.")),
    category=(
        one_of(value for value, _ in EXPENSE_CATEGORY_CHOICES) | None,
        Field(default=None, description="Only the expenses in this category; absent, every category."),
    ),
)


class ExpenseRow(TypedDict):
    """One expense; each related record by its slug or id, or null."""

    id: Annotated[int, Describe("The expense's id, which its page and the records linked to it name it by.")]
    date: Annotated[str | None, Describe("When it was spent, an ISO 8601 date; null when not recorded.")]
    vendor: ExpenseVendor
    item: ExpenseItem
    category: Annotated[
        str, Describe(ended(f"What type of expense it is: {listed(value for value, _ in EXPENSE_CATEGORY_CHOICES)}"))
    ]
    total_cost: TotalCostAmount
    business_use_percentage: BusinessUseShare
    estimated_deductible_amount: DeductibleAmount
    payment_method: PaymentMethod
    business_purpose: Annotated[str, Describe("Why the business needed it; empty when not recorded.")]
    notes: Notes
    related_project: Annotated[str | None, Describe("The slug of the project this was spent on; null for none.")]
    related_asset: Annotated[str | None, Describe("The slug of the asset this bought or kept running; null for none.")]
    related_content: Annotated[str | None, Describe("The slug of the writeup or page this paid for; null for none.")]
    related_documentation: Annotated[
        str | None,
        Describe("The vault doc ID of the doc that records it; null for none, or for one too sensitive to share."),
    ]
    paid_from: Annotated[str, Describe("The account that paid, as `type:key`; empty when not recorded.")]
    about: Annotated[
        str, Describe("What it was for, as `type:key`, where none of the related records says it; empty for none.")
    ]
    updated_at: UpdatedAt


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "expenses",
            "Expenses",
            "Expense records with stable relationship identifiers.",
            Capability.READ,
            read_models.list_expenses,
            ExpenseQuery,
            search=SearchDefinition(
                "expenses",
                Expense,
                "pk",
                ("vendor", "item", "category", "business_purpose", "notes"),
                label="Expenses",
            ),
            web_route="expenses:list",
            row_type=ExpenseRow,
        ),
    )
