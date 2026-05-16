"""Receipt views.

Receipt files are stored OUTSIDE the app and are never exposed via the public
media URL. The ``ReceiptFileView`` streams the file only to authenticated users.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
    View,
)

from core.audit import record_event
from core.models import AuditLog

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
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class ReceiptDetailView(LoginRequiredMixin, DetailView):
    model = Receipt
    template_name = "receipts/receipt_detail.html"
    context_object_name = "receipt"


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
