"""Receipt views.

Receipt files are stored OUTSIDE the app and are never exposed via the public
media URL. The ``ReceiptFileView`` streams the file only to authenticated users.
"""

from pathlib import Path

from django.contrib import messages
from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog
from hq.platform.application.money import money
from hq.platform.application.receipts import (
    ReceiptMetadataCommand,
    receipt_command_from_cleaned_data,
    update_receipt,
    upload_receipt,
)
from hq.platform.application.deletion import DeleteCommand
from hq.platform.application.records import deleter
from hq.platform.application.security import web_principal
from hq.platform.application.pages import PageAction, PageMixin, record_trail
from hq.platform.application.tables import TableColumn, TableListMixin, TableToggle
from hq.platform.application.moments import when

from hq.domains.expenses.models import Expense
from .forms import ReceiptUploadForm
from .validation import (
    ALLOWED_RECEIPT_CONTENT_TYPES,
    INLINE_SAFE_CONTENT_TYPES,
)
from .models import Receipt


RECEIPTS_TRAIL = ("Receipts", reverse_lazy("receipts:list"))


class ReceiptListView(PageMixin, TableListMixin, ListView):
    model = Receipt
    template_name = "receipts/receipt_list.html"
    paginate_by = 25
    page_title = "Receipts"
    table_search_scope = "receipts"
    table_selectable = True
    table_columns = (
        TableColumn("Vendor", "vendor"),
        TableColumn("Date", "date", "Oldest receipt date", "Newest receipt date"),
        TableColumn("Amount", "amount", "Lowest amount", "Highest amount", css="key-col"),
        TableColumn("For", css="key-col"),
        TableColumn("File", "original_filename"),
        TableColumn("Uploaded", "uploaded_at", "Least recently uploaded", "Recently uploaded"),
    )
    table_toggles = (TableToggle("unlinked", "No expense or asset"),)
    table_default_sort = "-uploaded_at"
    table_search_placeholder = "Search vendors, filenames, and notes…"

    def get_page_actions(self):
        return (PageAction("Upload receipt", reverse("receipts:create"), primary=True),)

    def get_queryset(self):
        qs = Receipt.objects.select_related("related_expense", "related_asset")
        if self.request.GET.get("unlinked"):
            qs = qs.filter(related_expense__isnull=True, related_asset__isnull=True)
        return self.apply_table_query(qs)




class ReceiptPage(PageMixin):
    """A page about one receipt, or a new one: its trail runs back to the list."""

    def get_receipt(self):
        return getattr(self, "object", None)

    def get_page_trail(self):
        return record_trail(RECEIPTS_TRAIL, self.get_receipt(), str)


class ReceiptDetailView(PageMixin, DetailView):
    queryset = Receipt.objects.select_related("related_expense", "related_asset")
    template_name = "receipts/receipt_detail.html"
    context_object_name = "receipt"

    def get_page_title(self):
        return self.object.label

    def get_page_lede(self):
        receipt = self.object
        named = f"{receipt.original_filename} · " if receipt.original_filename else ""
        return f"{named}uploaded {when(receipt.uploaded_at)}"

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


class ReceiptMatchView(ReceiptPage, TemplateView):
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
            money(receipt.amount),
        )

    def get_page_actions(self):
        return (PageAction("Cancel", self.get_receipt().get_absolute_url()),)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        receipt = self.get_receipt()

        # Only suggest if it's currently unlinked.
        if receipt.related_expense or receipt.related_asset:
            ctx["already_linked"] = True
            ctx["receipt"] = receipt
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
            messages.success(request, f"Receipt linked to {expense.label}.")

        return redirect(receipt.get_absolute_url())


class ReceiptCreateView(ReceiptPage, CreateView):
    page_title = "Upload receipt"
    model = Receipt
    form_class = ReceiptUploadForm
    template_name = "receipts/receipt_form.html"

    def get_initial(self):
        """Opened from an expense's page, the form starts on that expense."""

        initial = super().get_initial()
        asked = self.request.GET.get("related_expense", "")
        if asked.isdigit():
            initial["related_expense"] = int(asked)
        return initial

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


class ReceiptUpdateView(ReceiptPage, UpdateView):
    page_title = "Edit receipt"
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


class ReceiptDeleteView(ReceiptPage, DeleteView):
    page_title = "Delete receipt?"
    model = Receipt
    template_name = "receipts/receipt_confirm_delete.html"
    success_url = reverse_lazy("receipts:list")
    context_object_name = "receipt"

    def form_valid(self, form):
        receipt_id = self.get_object().pk
        deleter("receipts")(
            DeleteCommand(confirm=str(receipt_id)),
            principal=web_principal(self.request.user),
            current_id=receipt_id,
        )
        messages.success(self.request, "Receipt deleted.")
        return redirect(self.success_url)


class ReceiptFileView(View):
    """Auth-protected download of a receipt's underlying file."""

    def get(self, request, pk: int):
        receipt = get_object_or_404(Receipt, pk=pk)
        if not receipt.file:
            raise Http404("This receipt has no file.")
        try:
            path = Path(receipt.file.path)
        except (ValueError, NotImplementedError) as exc:
            raise Http404("This receipt's file cannot be opened from here.") from exc
        if not path.is_file():
            raise Http404("This receipt's file is missing.")

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
