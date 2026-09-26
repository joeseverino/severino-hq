from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.urls import reverse, reverse_lazy
from django.utils.formats import date_format
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.expenses import expense_command_from_cleaned_data, save_expense
from application.deletion import delete_expense
from application.pages import PageAction, PageMixin, record_trail
from application.tables import TableColumn, TableFilter, TableListMixin, TableToggle
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)
from .forms import ExpenseForm
from .models import EXPENSE_CATEGORY_CHOICES, Expense


class ExpenseListView(PageMixin, TableListMixin, LoginRequiredMixin, ListView):
    model = Expense
    template_name = "expenses/expense_list.html"
    paginate_by = 50
    page_title = "Expenses"
    table_search_scope = "expenses"
    table_selectable = True
    table_columns = (
        TableColumn("Date", "date", "Oldest expense", "Newest expense"),
        TableColumn("Vendor", "vendor", "Vendor A–Z", "Vendor Z–A"),
        TableColumn("Item", "item", "Item A–Z", "Item Z–A"),
        TableColumn("Category", "category", "Category", "Category reverse"),
        TableColumn("Cost", "total_cost", "Lowest cost", "Highest cost"),
        TableColumn("% biz", "business_use_percentage", "Lowest business use", "Highest business use"),
        TableColumn("Est. deduct.", "estimated_deductible_amount", "Lowest deductible", "Highest deductible"),
    )
    table_toggles = (TableToggle("no_receipts", "Missing receipt"),)
    table_default_sort = "-date"
    table_search_placeholder = "Search vendors, items, purpose, and notes…"

    def get_page_actions(self):
        return (PageAction("New expense", reverse("expenses:create"), primary=True),)

    def get_table_filters(self):
        years = [
            (date.year, str(date.year))
            for date in Expense.objects.dates("date", "year")
        ]
        return (
            TableFilter("category", "Category", "category", EXPENSE_CATEGORY_CHOICES),
            TableFilter("year", "Year", "date__year", years),
        )

    def get_queryset(self):
        qs = Expense.objects.all()
        if self.request.GET.get("no_receipts"):
            qs = qs.annotate(receipt_count=Count("receipts")).filter(receipt_count=0)
        return self.apply_table_query(qs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        totals = self.object_list.aggregate(
            total=Sum("total_cost"),
            deductible=Sum("estimated_deductible_amount"),
        )
        ctx.update(
            total_filtered=totals["total"] or Decimal("0.00"),
            deductible_filtered=totals["deductible"] or Decimal("0.00"),
        )
        return ctx


EXPENSES_TRAIL = ("Expenses", reverse_lazy("expenses:list"))


class ExpensePage(PageMixin):
    """A page about one expense, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        return record_trail(EXPENSES_TRAIL, getattr(self, "object", None), lambda expense: str(expense))


class ExpenseDetailView(PageMixin, LoginRequiredMixin, DetailView):
    model = Expense
    template_name = "expenses/expense_detail.html"
    context_object_name = "expense"
    queryset = Expense.objects.select_related(
        "related_project",
        "related_asset",
        "related_content",
        "related_documentation",
    ).prefetch_related("receipts")

    def get_page_title(self):
        return f"{self.object.vendor} · {self.object.item}"

    def get_page_lede(self):
        return f"{date_format(self.object.date)} · {self.object.get_category_display()}"

    def get_page_trail(self):
        return (EXPENSES_TRAIL,)

    def get_page_actions(self):
        pk = self.object.pk
        return (
            PageAction("Edit", reverse("expenses:edit", args=[pk])),
            PageAction("Delete", reverse("expenses:delete", args=[pk]), danger=True),
        )


class ExpenseWrite:
    """What every expense write shares, whichever direction it goes.

    An expense is identified by its primary key, which the service payload
    spells ``id``: hence the two names for the one identity.
    """

    model = Expense
    noun = "Expense"
    result_key = "expense"
    identity_attr = "pk"
    identity_result_key = "id"
    identity_kwarg = "current_id"

    # An expense is logged, not created: the ledger vocabulary is the one the
    # rest of this domain already speaks.
    created_message = "Expense logged: {target}."
    updated_message = "Expense updated: {target}."
    deleted_message = "Expense deleted: {target}."


class ExpenseCreateView(
    ExpenseWrite, ExpensePage, ServiceCreateMixin, LoginRequiredMixin, CreateView
):
    page_title = "New expense"
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"
    service = staticmethod(save_expense)
    command_from_cleaned_data = staticmethod(expense_command_from_cleaned_data)


class ExpenseUpdateView(
    ExpenseWrite, ExpensePage, ServiceUpdateMixin, LoginRequiredMixin, UpdateView
):
    page_title = "Edit expense"
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"
    service = staticmethod(save_expense)
    command_from_cleaned_data = staticmethod(expense_command_from_cleaned_data)


class ExpenseDeleteView(
    ExpenseWrite, ExpensePage, ServiceDeleteMixin, LoginRequiredMixin, DeleteView
):
    page_title = "Delete expense?"
    template_name = "expenses/expense_confirm_delete.html"
    success_url = reverse_lazy("expenses:list")
    context_object_name = "expense"
    service = staticmethod(delete_expense)
