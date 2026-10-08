"""Expense commands shared by web, MCP, and CLI."""

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Sum

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project

from hq.platform.core.audit import operation_context
from .sensitivity import SAFE_SENSITIVITIES
from .domains import records_of
from .security import Principal



@dataclass(frozen=True)
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


@dataclass(frozen=True)
class CategoryTotal:
    """What one category of a list of costs adds up to, and the list narrowed to it."""

    label: str
    total: Decimal
    url: str


def costs_by_category(
    rows, *, query, choices, narrowed: bool
) -> tuple[CostTotals, tuple[CategoryTotal, ...]]:
    """What a list of expenses adds up to, whole and by category, from one statement.

    ``rows`` is the list as its filters leave it and ``query`` the request's
    own, which each category's link keeps. The categories are said only where
    they divide the list: two or more, and none already chosen.
    """

    from hq.domains.expenses.models import Expense

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
        CategoryTotal(labels.get(row["category"], row["category"]), row["total"] or zero, narrowed_to(row["category"]))
        for row in sorted(sums, key=lambda row: row["total"] or zero, reverse=True)
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
    # ``kind:identity`` of the account that paid, and of what it was for.
    paid_from: str = ""
    about: str = ""


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
        "paid_from": expense.paid_from,
        "about": expense.about,
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
    principal.require(records_of("expenses").write)
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
