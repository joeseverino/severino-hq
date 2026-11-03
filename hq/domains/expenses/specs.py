"""The expenses resource, declared on its domain in application/domains.py."""

from typing import TypedDict

from pydantic import Field, create_model

from hq.platform.application import read_models
from hq.platform.application.integration_specs import ResourceSpec
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

    id: int
    date: str | None
    vendor: str
    item: str
    category: str
    # Money, as a decimal string.
    total_cost: str
    business_use_percentage: int
    estimated_deductible_amount: str
    payment_method: str
    business_purpose: str
    notes: str
    related_project: str | None
    related_asset: str | None
    related_content: str | None
    related_documentation: str | None
    paid_from: str
    about: str
    updated_at: str


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
