"""The expenses resource, declared on its domain in application/domains.py."""

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
        ),
    )
