"""Receipt views.

Receipt files are stored OUTSIDE the app and are never exposed via the public
media URL. The ``ReceiptFileView`` streams the file only to authenticated users.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from core.audit import record_event
from core.models import AuditLog
from application.receipts import (
    ReceiptMetadataCommand,
    receipt_command_from_cleaned_data,
    update_receipt,
    upload_receipt,
)
from application.deletion import DeleteCommand, delete_receipt
from application.security import web_principal
from application.tables import TableListMixin, TableSort, TableToggle

from expenses.models import Expense
from .forms import ReceiptUploadForm
from .models import Receipt


class ReceiptListView(TableListMixin, LoginRequiredMixin, ListView):
    model = Receipt
    template_name = "receipts/receipt_list.html"
    context_object_name = "receipts_list"
    paginate_by = 25
    table_search_scope = "receipts"
    table_sorts = (
        TableSort("-uploaded_at", "Recently uploaded", "-uploaded_at"),
        TableSort("-date", "Newest receipt date", "-date"),
        TableSort("date", "Oldest receipt date", "date"),
        TableSort("vendor", "Vendor A–Z", "vendor"),
        TableSort("-vendor", "Vendor Z–A", "-vendor"),
        TableSort("-amount", "Highest amount", "-amount"),
        TableSort("amount", "Lowest amount", "amount"),
        TableSort("original_filename", "Filename A–Z", "original_filename"),
        TableSort("-original_filename", "Filename Z–A", "-original_filename"),
        TableSort("uploaded_at", "Least recently uploaded", "uploaded_at"),
    )
    table_toggles = (TableToggle("unlinked", "Unlinked only"),)
    table_default_sort = "-uploaded_at"
    table_search_placeholder = "Search vendors, filenames, and notes…"

    def get_queryset(self):
        qs = Receipt.objects.select_related("related_expense", "related_asset")
        if self.request.GET.get("unlinked"):
            qs = qs.filter(related_expense__isnull=True, related_asset__isnull=True)
        return self.apply_table_query(qs)


class ReceiptDetailView(LoginRequiredMixin, DetailView):
    model = Receipt
    template_name = "receipts/receipt_detail.html"
    context_object_name = "receipt"


class ReceiptMatchView(LoginRequiredMixin, TemplateView):
    """Suggest potential Expense links for an unlinked receipt."""

    template_name = "receipts/receipt_match.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        receipt = get_object_or_404(Receipt, pk=self.kwargs["pk"])

        # Only suggest if it's currently unlinked.
        if receipt.related_expense or receipt.related_asset:
            ctx["already_linked"] = True
            return ctx

        # Find potential expenses with the same vendor or same amount.
        potential_expenses = Expense.objects.annotate(
            receipt_count=Count("receipts")
        ).filter(receipt_count=0)

        # Refine matches: same amount is a strong signal, same vendor is a decent signal.
        matches = []
        if receipt.amount > 0:
            matches = list(potential_expenses.filter(total_cost=receipt.amount))

        if not matches and receipt.vendor:
            matches = list(potential_expenses.filter(vendor__icontains=receipt.vendor))

        ctx.update(
            receipt=receipt,
            matches=matches[:10],
        )
        return ctx

    def post(self, request, *args, **kwargs):
        receipt = get_object_or_404(Receipt, pk=self.kwargs["pk"])
        expense_id = request.POST.get("expense_id")

        if expense_id:
            expense = get_object_or_404(Expense, pk=expense_id)
            update_receipt(
                ReceiptMetadataCommand(
                    vendor=receipt.vendor,
                    date=receipt.date,
                    amount=receipt.amount,
                    notes=receipt.notes,
                    related_expense=expense.id,
                    related_asset=(
                        receipt.related_asset.slug if receipt.related_asset else None
                    ),
                ),
                principal=web_principal(request.user),
                current_id=receipt.id,
            )
            messages.success(request, f"Receipt linked to expense: {expense}")

        return redirect(receipt.get_absolute_url())


class ReceiptCreateView(LoginRequiredMixin, CreateView):
    model = Receipt
    form_class = ReceiptUploadForm
    template_name = "receipts/receipt_form.html"

    def form_valid(self, form):
        upload = form.cleaned_data["file"]
        result = upload_receipt(
            receipt_command_from_cleaned_data(form.cleaned_data),
            upload,
            principal=web_principal(self.request.user),
        )
        self.object = Receipt.objects.get(pk=result["receipt"]["id"])
        messages.success(self.request, "Receipt uploaded.")
        return redirect(self.object.get_absolute_url())


class ReceiptUpdateView(LoginRequiredMixin, UpdateView):
    model = Receipt
    form_class = ReceiptUploadForm
    template_name = "receipts/receipt_form.html"

    def form_valid(self, form):
        result = update_receipt(
            receipt_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
            current_id=self.get_object().pk,
            upload=form.cleaned_data.get("file"),
        )
        self.object = Receipt.objects.get(pk=result["receipt"]["id"])
        messages.success(self.request, "Receipt updated.")
        return redirect(self.object.get_absolute_url())


class ReceiptDeleteView(LoginRequiredMixin, DeleteView):
    model = Receipt
    template_name = "receipts/receipt_confirm_delete.html"
    success_url = reverse_lazy("receipts:list")
    context_object_name = "receipt"

    def form_valid(self, form):
        receipt_id = self.get_object().pk
        delete_receipt(
            DeleteCommand(confirm=str(receipt_id)),
            principal=web_principal(self.request.user),
            current_id=receipt_id,
        )
        messages.success(self.request, "Receipt deleted.")
        return redirect(self.success_url)


class ReceiptFileView(LoginRequiredMixin, View):
    """Auth-protected download of a receipt's underlying file."""

    def get(self, request, pk: int):
        receipt = get_object_or_404(Receipt, pk=pk)
        if not receipt.file:
            raise Http404("Receipt has no attached file.")
        try:
            path = Path(receipt.file.path)
        except (ValueError, NotImplementedError) as exc:
            raise Http404("Receipt file is not on a streamable backend.") from exc
        if not path.is_file():
            raise Http404("Receipt file not found on disk.")

        record_event(
            action=AuditLog.Action.VIEWED,
            obj=receipt,
            type_label="Receipt",
            message=f"Receipt file viewed: {receipt.original_filename}",
        )

        content_type = (
            receipt.content_type
            or mimetypes.guess_type(receipt.original_filename or path.name)[0]
            or "application/octet-stream"
        )
        response = FileResponse(
            path.open("rb"),
            content_type=content_type,
            filename=receipt.original_filename or path.name,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
