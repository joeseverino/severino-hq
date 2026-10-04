"""The expenses resource, declared on its domain in application/domains.py."""

from __future__ import annotations

from pydantic import Field

from application import read_models
from application.integration_specs import ResourceSpec
from application.resources import BoundedQuery
from application.search_contracts import SearchDefinition
from application.security import Capability

from .models import Expense


class ExpenseQuery(BoundedQuery):
    year: int | None = Field(default=None, ge=1, le=9999)
    category: str | None = None


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
