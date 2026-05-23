"""Receipt views.

Receipt files are stored OUTSIDE the app and are never exposed via the public
media URL. The ``ReceiptFileView`` streams the file only to authenticated users.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
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

from expenses.models import Expense
from .forms import ReceiptUploadForm
from .models import Receipt


class ReceiptListView(LoginRequiredMixin, ListView):
    model = Receipt
    template_name = "receipts/receipt_list.html"
    context_object_name = "receipts_list"
    paginate_by = 25

    def get_queryset(self):
        qs = Receipt.objects.select_related("related_expense", "related_asset")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(vendor__icontains=q)
                | Q(notes__icontains=q)
                | Q(original_filename__icontains=q)
            )
        if self.request.GET.get("unlinked"):
            qs = qs.filter(related_expense__isnull=True, related_asset__isnull=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        ctx["unlinked"] = self.request.GET.get("unlinked", "")
        return ctx


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
            receipt.related_expense = expense
            receipt.save(update_fields=["related_expense"])

            record_event(
                action=AuditLog.Action.UPDATED,
                obj=receipt,
                type_label="Receipt",
                message=f"Linked receipt to expense: {expense}",
                metadata={"expense_id": expense.id},
            )
            messages.success(request, f"Receipt linked to expense: {expense}")

        return redirect(receipt.get_absolute_url())


class ReceiptCreateView(LoginRequiredMixin, CreateView):
    model = Receipt
    form_class = ReceiptUploadForm
    template_name = "receipts/receipt_form.html"

    def form_valid(self, form):
        receipt: Receipt = form.save(commit=False)
        upload = form.cleaned_data["file"]
        receipt.original_filename = upload.name[:255]
        receipt.content_type = getattr(upload, "content_type", "") or ""
        receipt.size_bytes = upload.size or 0
        receipt.save()
        record_event(
            action=AuditLog.Action.UPLOADED,
            obj=receipt,
            type_label="Receipt",
            message=f"Receipt uploaded: {receipt.original_filename}",
            metadata={
                "size_bytes": receipt.size_bytes,
                "content_type": receipt.content_type,
            },
        )
        self.object = receipt
        messages.success(self.request, "Receipt uploaded.")
        return super().form_valid(form)


class ReceiptUpdateView(LoginRequiredMixin, UpdateView):
    model = Receipt
    form_class = ReceiptUploadForm
    template_name = "receipts/receipt_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Receipt updated.")
        return response


class ReceiptDeleteView(LoginRequiredMixin, DeleteView):
    model = Receipt
    template_name = "receipts/receipt_confirm_delete.html"
    success_url = reverse_lazy("receipts:list")
    context_object_name = "receipt"

    def form_valid(self, form):
        receipt: Receipt = self.get_object()
        stored_path = None
        if receipt.file:
            try:
                stored_path = Path(receipt.file.path)
            except (ValueError, NotImplementedError):
                stored_path = None
        response = super().form_valid(form)
        if stored_path and stored_path.is_file():
            try:
                stored_path.unlink()
            except OSError:
                pass
        messages.success(self.request, "Receipt deleted.")
        return response


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
