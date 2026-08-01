from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.expenses import expense_command_from_cleaned_data, save_expense
from application.deletion import DeleteCommand, delete_expense
from application.security import web_principal
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
from .forms import ExpenseForm
from .models import EXPENSE_CATEGORY_CHOICES, Expense


class ExpenseListView(TableListMixin, LoginRequiredMixin, ListView):
    model = Expense
    template_name = "expenses/expense_list.html"
    context_object_name = "expenses_list"
    paginate_by = 50
    table_search_scope = "expenses"
    table_sorts = (
        TableSort("-date", "Newest expense", "-date"),
        TableSort("date", "Oldest expense", "date"),
        TableSort("vendor", "Vendor A–Z", "vendor"),
        TableSort("-vendor", "Vendor Z–A", "-vendor"),
        TableSort("item", "Item A–Z", "item"),
        TableSort("-item", "Item Z–A", "-item"),
        TableSort("-total_cost", "Highest cost", "-total_cost"),
        TableSort("total_cost", "Lowest cost", "total_cost"),
        TableSort("category", "Category", "category"),
        TableSort("-category", "Category reverse", "-category"),
        TableSort(
            "-estimated_deductible_amount",
            "Highest deductible",
            "-estimated_deductible_amount",
        ),
        TableSort(
            "estimated_deductible_amount",
            "Lowest deductible",
            "estimated_deductible_amount",
        ),
        TableSort(
            "business_use_percentage", "Lowest business use", "business_use_percentage"
        ),
        TableSort(
            "-business_use_percentage",
            "Highest business use",
            "-business_use_percentage",
        ),
    )
    table_toggles = (TableToggle("no_receipts", "Missing receipt"),)
    table_default_sort = "-date"
    table_search_placeholder = "Search vendors, items, purpose, and notes…"

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


class ExpenseDetailView(LoginRequiredMixin, DetailView):
    model = Expense
    template_name = "expenses/expense_detail.html"
    context_object_name = "expense"
    queryset = Expense.objects.select_related(
        "related_project",
        "related_asset",
        "related_content",
        "related_documentation",
    ).prefetch_related("receipts")


class ExpenseCreateView(LoginRequiredMixin, CreateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"

    def form_valid(self, form):
        result = save_expense(
            expense_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
        )
        self.object = Expense.objects.get(pk=result["expense"]["id"])
        messages.success(self.request, f"Expense logged: {self.object}.")
        return redirect(self.object.get_absolute_url())


class ExpenseUpdateView(LoginRequiredMixin, UpdateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"

    def form_valid(self, form):
        result = save_expense(
            expense_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
            current_id=self.get_object().pk,
        )
        self.object = Expense.objects.get(pk=result["expense"]["id"])
        messages.success(self.request, f"Expense updated: {self.object}.")
        return redirect(self.object.get_absolute_url())


class ExpenseDeleteView(LoginRequiredMixin, DeleteView):
    model = Expense
    template_name = "expenses/expense_confirm_delete.html"
    success_url = reverse_lazy("expenses:list")
    context_object_name = "expense"

    def form_valid(self, form):
        expense_id = self.get_object().pk
        result = delete_expense(
            DeleteCommand(confirm=str(expense_id)),
            principal=web_principal(self.request.user),
            current_id=expense_id,
        )
        messages.success(
            self.request, f"Expense deleted: {result['deleted']['label']}."
        )
        return redirect(self.success_url)
