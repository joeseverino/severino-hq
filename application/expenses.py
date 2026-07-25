"""Expense commands shared by web, MCP, and CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction

from assets.models import Asset
from content.models import ContentItem
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project

from core.audit import operation_context
from .security import Capability, Principal

SAFE_SENSITIVITIES = (
    DocumentationRecord.Sensitivity.PUBLIC,
    DocumentationRecord.Sensitivity.INTERNAL,
)


class NotFoundError(ValueError):
    pass


class ConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ExpenseCommand:
    date: date
    vendor: str
    item: str
    category: str = "miscellaneous"
    total_cost: Decimal = Decimal("0.00")
    business_use_percentage: int = 100
    payment_method: str = ""
    business_purpose: str = ""
    notes: str = ""
    related_project: str | None = None
    related_asset: str | None = None
    related_content: str | None = None
    related_documentation: str | None = None


def serialize_expense(expense: Expense) -> dict[str, Any]:
    doc = expense.related_documentation
    return {
        "id": expense.id,
        "date": expense.date.isoformat(),
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
        "related_documentation": (
            doc.doc_id if doc and doc.sensitivity in SAFE_SENSITIVITIES else None
        ),
        "updated_at": expense.updated_at.isoformat(),
    }


def _one(model, field: str, value):
    if value in (None, ""):
        return None
    try:
        return model.objects.get(**{field: value})
    except model.DoesNotExist as exc:
        raise NotFoundError(f"Related {model._meta.verbose_name} {value!r} was not found.") from exc


@transaction.atomic
def save_expense(
    command: ExpenseCommand,
    *,
    principal: Principal,
    current_id: int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(Capability.WRITE_EXPENSES)
    operation = "expense.create" if current_id is None else "expense.update"
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        if current_id is None:
            expense, created = Expense(), True
        else:
            try:
                expense = Expense.objects.select_for_update().get(pk=current_id)
            except Expense.DoesNotExist as exc:
                raise NotFoundError(f"Expense {current_id!r} was not found.") from exc
            created = False
            if expected_updated_at and expense.updated_at.isoformat() != expected_updated_at:
                raise ConflictError(f"Expense {current_id!r} changed after it was read.")

        values = asdict(command)
        relations = {
            "related_project": _one(Project, "slug", values.pop("related_project")),
            "related_asset": _one(Asset, "slug", values.pop("related_asset")),
            "related_content": _one(ContentItem, "slug", values.pop("related_content")),
            "related_documentation": _one(
                DocumentationRecord, "doc_id", values.pop("related_documentation")
            ),
        }
        for field, value in {**values, **relations}.items():
            setattr(expense, field, value)
        expense.full_clean()
        expense.save()
    return {"ok": True, "created": created, "expense": serialize_expense(expense)}


def expense_command_from_cleaned_data(data: dict[str, Any]) -> ExpenseCommand:
    return ExpenseCommand(
        date=data["date"],
        vendor=data["vendor"],
        item=data["item"],
        category=data["category"],
        total_cost=data["total_cost"],
        business_use_percentage=data["business_use_percentage"],
        payment_method=data["payment_method"],
        business_purpose=data["business_purpose"],
        notes=data["notes"],
        related_project=data["related_project"].slug if data["related_project"] else None,
        related_asset=data["related_asset"].slug if data["related_asset"] else None,
        related_content=data["related_content"].slug if data["related_content"] else None,
        related_documentation=(
            data["related_documentation"].doc_id
            if data["related_documentation"]
            else None
        ),
    )
