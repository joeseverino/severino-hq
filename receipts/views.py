"""Receipt views.

Receipt files are stored OUTSIDE the app and are never exposed via the public
media URL. The ``ReceiptFileView`` streams the file only to authenticated users.
"""

from __future__ import annotations

from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.contrib.humanize.templatetags.humanize import intcomma
from django.template.defaultfilters import floatformat
from django.urls import reverse, reverse_lazy
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.timezone import localtime
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
from application.pages import PageAction, PageMixin
from application.tables import TableColumn, TableListMixin, TableToggle

from expenses.models import Expense
from .forms import ReceiptUploadForm
from .validation import (
    ALLOWED_RECEIPT_CONTENT_TYPES,
    INLINE_SAFE_CONTENT_TYPES,
)
from .models import Receipt


RECEIPTS_TRAIL = ("Receipts", reverse_lazy("receipts:list"))
PRIVATE_FILES = "Files are private. Only signed-in users can open them."


class ReceiptListView(PageMixin, TableListMixin, LoginRequiredMixin, ListView):
    model = Receipt
    template_name = "receipts/receipt_list.html"
    paginate_by = 25
    page_title = "Receipts"
    page_lede = PRIVATE_FILES
    table_search_scope = "receipts"
    table_selectable = True
    table_columns = (
        TableColumn("Uploaded", "uploaded_at", "Least recently uploaded", "Recently uploaded"),
        TableColumn("Vendor", "vendor", "Vendor A–Z", "Vendor Z–A"),
        TableColumn("Date", "date", "Oldest receipt date", "Newest receipt date"),
        TableColumn("Amount", "amount", "Lowest amount", "Highest amount"),
        TableColumn("Filename", "original_filename", "Filename A–Z", "Filename Z–A"),
        TableColumn("Links"),
    )
    table_toggles = (TableToggle("unlinked", "Unlinked only"),)
    table_default_sort = "-uploaded_at"
    table_search_placeholder = "Search vendors, filenames, and notes…"

    def get_page_actions(self):
        return (PageAction("Upload receipt", reverse("receipts:create"), primary=True),)

    def get_queryset(self):
        qs = Receipt.objects.select_related("related_expense", "related_asset")
        if self.request.GET.get("unlinked"):
            qs = qs.filter(related_expense__isnull=True, related_asset__isnull=True)
        return self.apply_table_query(qs)


def dollars(amount) -> str:
    return f"${intcomma(floatformat(amount, 2))}"


class ReceiptPage(PageMixin):
    """A page about one receipt, or a new one: its trail runs back to the list."""

    def get_receipt(self):
        return getattr(self, "object", None)

    def get_page_trail(self):
        receipt = self.get_receipt()
        if receipt is None:
            return (RECEIPTS_TRAIL,)
        return (RECEIPTS_TRAIL, (str(receipt), receipt.get_absolute_url()))


class ReceiptDetailView(PageMixin, LoginRequiredMixin, DetailView):
    model = Receipt
    template_name = "receipts/receipt_detail.html"
    context_object_name = "receipt"

    def get_page_title(self):
        return self.object.vendor or "Receipt"

    def get_page_lede(self):
        receipt = self.object
        return (
            f"{receipt.original_filename or 'file'} · "
            f"uploaded {date_format(localtime(receipt.uploaded_at), 'DATETIME_FORMAT')}"
        )

    def get_page_trail(self):
        return (RECEIPTS_TRAIL,)

    def get_page_actions(self):
        receipt = self.object
        actions = []
        if not receipt.related_expense and not receipt.related_asset:
            actions.append(
                PageAction(
                    "Link to expense",
                    reverse("receipts:match", args=[receipt.pk]),
                    primary=True,
                )
            )
        actions += [
            PageAction("Download file", reverse("receipts:file", args=[receipt.pk])),
            PageAction("Edit", reverse("receipts:edit", args=[receipt.pk])),
            PageAction(
                "Delete", reverse("receipts:delete", args=[receipt.pk]), danger=True
            ),
        ]
        return tuple(actions)


class ReceiptMatchView(ReceiptPage, LoginRequiredMixin, TemplateView):
    """Suggest potential Expense links for an unlinked receipt."""

    template_name = "receipts/receipt_match.html"
    page_title = "Link receipt to expense"

    def get_receipt(self):
        if not hasattr(self, "receipt"):
            self.receipt = get_object_or_404(Receipt, pk=self.kwargs["pk"])
        return self.receipt

    def get_page_lede(self):
        receipt = self.get_receipt()
        return format_html(
            "<strong>{}</strong> · {}",
            receipt.vendor or "(Unknown vendor)",
            dollars(receipt.amount),
        )

    def get_page_actions(self):
        return (PageAction("Cancel", self.get_receipt().get_absolute_url()),)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        receipt = self.get_receipt()

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


class ReceiptCreateView(ReceiptPage, LoginRequiredMixin, CreateView):
    page_title = "Upload receipt"
    page_lede = PRIVATE_FILES
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


class ReceiptUpdateView(ReceiptPage, LoginRequiredMixin, UpdateView):
    page_title = "Edit receipt"
    page_lede = PRIVATE_FILES
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


class ReceiptDeleteView(ReceiptPage, LoginRequiredMixin, DeleteView):
    page_title = "Delete receipt?"
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

        # Never guessed from the filename, which the uploader chooses. The
        # stored type is used only if it is one this app agreed to accept;
        # anything else, including a blank left by an older upload, is served
        # as an opaque byte stream.
        stored = (receipt.content_type or "").strip().lower()
        allowed = stored in ALLOWED_RECEIPT_CONTENT_TYPES
        content_type = stored if allowed else "application/octet-stream"
        response = FileResponse(
            path.open("rb"),
            content_type=content_type,
            # Rendered in place only for the formats meant to be looked at.
            # Downloading the rest costs a click and removes the question of
            # what a browser might decide to do with it.
            as_attachment=content_type not in INLINE_SAFE_CONTENT_TYPES,
            filename=receipt.original_filename or path.name,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
