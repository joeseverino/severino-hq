from typing import override

from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    TemplateView,
    UpdateView,
)

from hq.platform.application import related
from hq.platform.application.documentation import document_link
from hq.platform.application.entity_links import entity_link
from hq.platform.application.expenses import category_figures, said_totals
from hq.platform.application.labels import dotted
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import stamp_day, when_day
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin
from hq.platform.application.references import referenced_by_row, resolve_many
from hq.platform.application.security import web_principal
from hq.platform.application.tables import Cell, TableColumn
from hq.platform.application.ui import measured
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin

from .models import EXPENSE_CATEGORY_CHOICES, Expense


class ExpenseListView(ListPageMixin, TemplateView):
    resource = "expenses"
    table_empty = "No expenses yet."
    template_name = "expenses/expense_list.html"
    page_size = 50
    page_title = "Expenses"
    table_selectable = True
    table_columns = (
        TableColumn(
            "Vendor",
            "vendor",
            cell=lambda row: Cell.of(entity_link("expense", row["id"], label=row["vendor"])),
        ),
        TableColumn("Date", "date", cell=lambda row: Cell(moment=stamp_day(row["date"]))),
        TableColumn("Item", "item", cell=lambda row: Cell(row["item"] or "")),
        TableColumn("Category", "category", cell=lambda row: Cell(row["category_label"])),
        TableColumn("Cost", "total_cost", css="key-col", cell=lambda row: Cell(amount=row["total_cost"])),
        TableColumn(
            "Business use",
            "business_use_percentage",
            cell=lambda row: Cell(measured(row["business_use_percentage"], "%"), muted=True),
        ),
        TableColumn(
            "Deductible (est.)",
            "estimated_deductible_amount",
            cell=lambda row: Cell(amount=row["estimated_deductible_amount"]),
        ),
    )
    table_search_placeholder = "Search vendors, items, purpose, and notes…"

    @override
    def get_page_actions(self):
        return (PageAction.command("expense.create", primary=True),)

    @override
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        totals = self.collection["totals"]
        ctx["totals"] = said_totals(totals, narrowed=bool(ctx["table"]["active_count"]))
        ctx["by_category"] = category_figures(
            totals["categories"], query=self.request.GET, choices=EXPENSE_CATEGORY_CHOICES
        )
        return ctx


EXPENSES_TRAIL = (("Expenses", reverse_lazy("expenses:list")),)


class ExpensePage(PageMixin):
    """A page about one expense, or a new one: its trail runs back to the list."""

    model = Expense
    page_trail = EXPENSES_TRAIL

    @override
    def trail_label(self, record):
        return str(record)


class ExpenseDetailView(RecordPageMixin, TemplateView):
    resource = "expenses"
    context_object_name = "expense"
    url_kwarg = "pk"
    page_trail = EXPENSES_TRAIL
    template_name = "expenses/expense_detail.html"
    model = Expense
    stored_related = ("receipts",)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record, principal = self.record, web_principal(self.request.user)
        named = (
            ("project", record["related_project"], record["project_name"]),
            ("asset", record["related_asset"], record["asset_name"]),
            ("writeup", record["related_content"], record["content_title"]),
        )
        context["links"] = {kind: entity_link(kind, key, label=name) if key else None for kind, key, name in named}
        doc_id = record["related_documentation"]
        context["document"] = document_link(doc_id, record["document_title"]) if doc_id else None
        # The records of other kinds that name this one, read by it, and the
        # saved names of its references, which its record leaves to their owner.
        found = self.stored
        context["account"], context["about"] = resolve_many(
            [(record["paid_from"], found.paid_from_name), (record["about"], found.about_name)],
            principal=principal,
        )
        receipts = related.receipts(found.receipts.all())
        context["receipted"] = bool(receipts.items)
        context["related"] = related.related(receipts, *referenced_by_row(found, principal=principal))
        return context

    @override
    def get_page_title(self):
        return self.record["label"]

    @override
    def get_page_lede(self):
        return dotted(when_day(stamp_day(self.record["date"])), self.record["category_label"])

    @override
    def get_page_actions(self):
        pk = self.record["id"]
        return (
            PageAction.command("expense.update", pk),
            PageAction.command("expense.delete", pk),
        )


# What saving the expense's form does, said under it.
FORM_PAGE = {"form_note": "Deductible estimate = cost × business use. Updated when you save."}


class ExpenseCreateView(ExpensePage, RecordFormMixin, CreateView):
    page_title = "New expense"
    extra_context = FORM_PAGE

    @override
    def get_initial(self):
        """Opened from a receipt with no expense, the form starts with what the receipt says."""

        initial = super().get_initial()
        for field, name in (("vendor", "vendor"), ("total_cost", "cost")):
            if value := self.request.GET.get(name, "").strip():
                initial[field] = value
        return initial


class ExpenseUpdateView(ExpensePage, RecordFormMixin, UpdateView):
    page_title = "Edit expense"
    extra_context = FORM_PAGE


class ExpenseDeleteView(ExpensePage, RecordDeleteMixin, DeleteView):
    page_title = "Delete expense?"
    template_name = "expenses/expense_confirm_delete.html"
    success_url = reverse_lazy("expenses:list")
    context_object_name = "expense"
