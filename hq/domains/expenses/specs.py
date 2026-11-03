"""The expenses resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field, create_model

from hq.platform.application import expenses, read_models
from hq.platform.application.answer_fields import BusinessUseShare, DeductibleAmount, TotalCostAmount, UpdatedAt
from hq.platform.application.assets import PaymentMethod
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.record_fields import Notes
from hq.platform.application.resources import BoundedQuery, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

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
