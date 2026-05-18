from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q, Sum
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from .forms import ExpenseForm
from .models import EXPENSE_CATEGORY_CHOICES, Expense


class ExpenseListView(LoginRequiredMixin, ListView):
    model = Expense
    template_name = "expenses/expense_list.html"
    context_object_name = "expenses_list"
    paginate_by = 50

    def get_queryset(self):
        qs = Expense.objects.all()
        q = self.request.GET.get("q", "").strip()
        category = self.request.GET.get("category", "").strip()
        year = self.request.GET.get("year", "").strip()
        sort = self.request.GET.get("sort", "-date")
        if q:
            qs = qs.filter(
                Q(vendor__icontains=q)
                | Q(item__icontains=q)
                | Q(business_purpose__icontains=q)
                | Q(notes__icontains=q)
            )
        if category:
            qs = qs.filter(category=category)
        if year and year.isdigit():
            qs = qs.filter(date__year=int(year))
        if sort in {
            "date", "-date", "vendor", "-vendor",
            "total_cost", "-total_cost", "category", "-category",
            "estimated_deductible_amount", "-estimated_deductible_amount",
        }:
            qs = qs.order_by(sort)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        totals = self.object_list.aggregate(
            total=Sum("total_cost"),
            deductible=Sum("estimated_deductible_amount"),
        )
        ctx.update(
            q=self.request.GET.get("q", ""),
            selected_category=self.request.GET.get("category", ""),
            selected_year=self.request.GET.get("year", ""),
            sort=self.request.GET.get("sort", "-date"),
            category_choices=EXPENSE_CATEGORY_CHOICES,
            total_filtered=totals["total"] or Decimal("0.00"),
            deductible_filtered=totals["deductible"] or Decimal("0.00"),
            available_years=Expense.objects.dates("date", "year"),
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
        response = super().form_valid(form)
        messages.success(self.request, f"Expense logged: {self.object}.")
        return response


class ExpenseUpdateView(LoginRequiredMixin, UpdateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "expenses/expense_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Expense updated: {self.object}.")
        return response


class ExpenseDeleteView(LoginRequiredMixin, DeleteView):
    model = Expense
    template_name = "expenses/expense_confirm_delete.html"
    success_url = reverse_lazy("expenses:list")
    context_object_name = "expense"

    def form_valid(self, form):
        label = str(self.get_object())
        response = super().form_valid(form)
        messages.success(self.request, f"Expense deleted: {label}.")
        return response
