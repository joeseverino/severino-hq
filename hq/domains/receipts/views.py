"""Receipt views.

Receipt files are stored OUTSIDE the app and are never exposed via the public
media URL. The ``ReceiptFileView`` streams the file only to authenticated users.
"""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import override

from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from hq.domains.expenses.models import Expense
from hq.platform.application.labels import dotted, dotted_html, human_bytes
from hq.platform.application.moments import moment, when
from hq.platform.application.money import money
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin
from hq.platform.application.receipts import (
    REPLACE,
    UPDATE,
    UPLOAD,
    file_taken,
    serialize_receipt,
    served_type,
    stored_file,
)
from hq.platform.application.tables import TableColumn, TableListMixin, TableToggle
from hq.platform.application.url_query import with_query
from hq.platform.application.web_writes import execute_posted
from hq.platform.application.writes import CapabilityFormMixin, CommandFormPage, RecordDeleteMixin
from hq.platform.core.templatetags.value_tags import breakable
from hq.platform.core.web_refusals import refused

from .models import Receipt
from .validation import (
    INLINE_SAFE_CONTENT_TYPES,
    PICTURE_CONTENT_TYPES,
)

RECEIPTS_TRAIL = (("Receipts", reverse_lazy("receipts:list")),)
# The details a link keeps as they are, read back as the record's update takes them.
_DETAILS = ("vendor", "date", "amount", "notes", "related_asset")


class ReceiptListView(PageMixin, TableListMixin, ListView):
    table_empty = "No receipts yet."
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
        TableColumn("For"),
        TableColumn("File", "original_filename"),
        TableColumn("Uploaded", "uploaded_at", "Least recently uploaded", "Recently uploaded"),
    )
    table_toggles = (TableToggle("unlinked", "No expense or asset"),)
    table_default_sort = "-uploaded_at"
    table_search_placeholder = "Search vendors, filenames, and notes…"

    @override
    def get_page_actions(self):
        return (PageAction.command(UPLOAD, primary=True),)

    @override
    def get_queryset(self):
        qs = Receipt.objects.select_related("related_expense", "related_asset")
        if self.request.GET.get("unlinked"):
            qs = qs.filter(related_expense__isnull=True, related_asset__isnull=True)
        return self.apply_table_query(qs)


class ReceiptPage(PageMixin):
    """A page about one receipt, or a new one: its trail runs back to the list."""

    page_trail = RECEIPTS_TRAIL

    @override
    def trail_label(self, record):
        return str(record)


class ReceiptDetailView(RecordPageMixin, TemplateView):
    resource = "receipts"
    context_object_name = "receipt"
    url_kwarg = "pk"
    template_name = "receipts/receipt_detail.html"

    @override
    def get_page_title(self):
        return self.record["label"]

    @override
    def get_page_lede(self):
        record = self.record
        return dotted(record["original_filename"], f"uploaded {when(moment(record['uploaded_at']))}")

    @override
    def get_page_actions(self):
        record = self.record
        pk = record["id"]
        actions = []
        if not record["related_expense"] and not record["related_asset"]:
            actions.append(PageAction.command(UPDATE, pk, variant="expense", primary=True))
        stored = record["file"] == "stored"
        actions += [
            *([PageAction("Download file", reverse("receipts:file", args=[pk]))] if stored else []),
            PageAction.command("receipt.update", pk),
            PageAction.command("receipt.delete", pk),
        ]
        return tuple(actions)

    @override
    def get_context_data(self, **kwargs):
        record = self.record
        size = record["size_bytes"]
        return super().get_context_data(
            **kwargs,
            file_picture=record["file"] == "stored" and served_type(record["content_type"]) in PICTURE_CONTENT_TYPES,
            file_line=dotted(record["file_type_label"], human_bytes(size) if size else ""),
        )


class ReceiptMatchView(ReceiptPage, TemplateView):
    """Suggest potential Expense links for an unlinked receipt."""

    template_name = "receipts/receipt_match.html"
    page_title = "Link receipt"
    runs = ((UPDATE, "expense"), (UPDATE, "link"))

    @cached_property
    def receipt(self):
        return get_object_or_404(Receipt, pk=self.kwargs["pk"])

    @override
    def get(self, request, *args, **kwargs):
        # The page's trail reads the record the way a detail view holds it.
        self.object = self.receipt
        return super().get(request, *args, **kwargs)

    @override
    def get_page_lede(self):
        receipt = self.receipt
        return dotted_html(
            format_html("<strong>{}</strong>", breakable(receipt.vendor or "(Unknown vendor)")), money(receipt.amount)
        )

    @override
    def get_page_actions(self):
        return (PageAction("Cancel", self.receipt.get_absolute_url()),)

    @override
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        receipt = ctx["receipt"] = self.receipt

        # Only suggest if it's currently unlinked.
        if receipt.related_expense or receipt.related_asset:
            ctx["already_linked"] = True
            return ctx

        # With no match, a new expense starts from what the receipt says.
        ctx["new_expense"] = with_query(reverse("expenses:create"), {"vendor": receipt.vendor, "cost": receipt.amount})
        # Find potential expenses with the same vendor or same amount.
        potential_expenses = Expense.objects.annotate(receipt_count=Count("receipts")).filter(receipt_count=0)

        # Refine matches: same amount is a strong signal, same vendor is a decent signal.
        matches = []
        if receipt.amount > 0:
            matches = list(potential_expenses.filter(total_cost=receipt.amount))

        if not matches and receipt.vendor:
            matches = list(potential_expenses.filter(vendor__icontains=receipt.vendor))

        # Each match with its one control, which links it to this receipt.
        ctx.update(
            matches=[
                (
                    expense,
                    (
                        PageAction.command(
                            UPDATE, receipt.pk, variant="link", primary=True, field="expense_id", value=str(expense.pk)
                        ),
                    ),
                )
                for expense in matches[:10]
            ],
        )
        return ctx

    def post(self, request, *args, **kwargs):
        receipt = get_object_or_404(Receipt, pk=self.kwargs["pk"])
        expense_id = request.POST.get("expense_id")

        if expense_id:
            expense = get_object_or_404(Expense, pk=expense_id)
            details = dict(serialize_receipt(receipt))
            result = execute_posted(
                request,
                UPDATE,
                {field: details[field] for field in _DETAILS} | {"related_expense": expense.id},
                target=receipt.id,
            )
            refused(request, result)

        return redirect(receipt.get_absolute_url())


# The fields its form page draws beyond the form's own loop.
FORM_PAGE = {"form_fields": "receipts/_receipt_fields.html"}


class ReceiptFormPage(CommandFormPage, ReceiptPage, CapabilityFormMixin):
    """A receipt's form, said as the upload command's page says it: a new file
    is the upload's or the replace capability's, details alone the record's update."""

    creates = UPLOAD
    updates = UPDATE
    listed_route = "receipts:list"
    extra_context = FORM_PAGE
    model = Receipt
    result_key = "receipt"
    identity_attr = "pk"
    identity_result_key = "id"

    @override
    def capability(self, form):
        if not self.adding and form.cleaned_data.get("file") is not None:
            return REPLACE
        return super().capability(form)


class ReceiptCreateView(ReceiptFormPage, CreateView):
    page_title = "New receipt"

    @override
    def get_initial(self):
        """Opened from an expense's page, the form starts on that expense."""

        initial = super().get_initial()
        asked = self.request.GET.get("related_expense", "")
        if asked.isdigit():
            initial["related_expense"] = int(asked)
        return initial


class ReceiptUpdateView(ReceiptFormPage, UpdateView):
    page_title = "Edit receipt"

    @override
    def get_initial(self):
        """The file it holds, named and linked as HQ serves it: its storage is never served."""

        initial = super().get_initial()
        if self.object.file:
            initial["file"] = _StoredFile(
                self.object.original_filename or Path(self.object.file.name).name,
                reverse("receipts:file", args=[self.object.pk]),
            )
        return initial


@dataclass(frozen=True, slots=True)
class _StoredFile:
    """A receipt's file as its form's control names and links it."""

    name: str
    url: str

    def __str__(self) -> str:
        return self.name


class ReceiptDeleteView(ReceiptPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete receipt?"
    model = Receipt
    template_name = "receipts/receipt_confirm_delete.html"
    success_url = reverse_lazy("receipts:list")
    context_object_name = "receipt"


def _file_response(receipt: Receipt, path: Path, content_type: str, *, inline: bool) -> FileResponse:
    response = FileResponse(
        path.open("rb"),
        content_type=content_type,
        as_attachment=not inline,
        filename=receipt.original_filename or path.name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


class ReceiptFileView(View):
    """Auth-protected download of a receipt's underlying file."""

    def get(self, _request, pk: int):
        receipt = get_object_or_404(Receipt, pk=pk)
        if (path := stored_file(receipt)) is None:
            raise Http404("This receipt has no file HQ can open.")

        file_taken(receipt)

        content_type = served_type(receipt.content_type)
        # Rendered in place only for the formats meant to be looked at.
        # Downloading the rest costs a click and removes the question of what
        # a browser might decide to do with it.
        return _file_response(receipt, path, content_type, inline=content_type in INLINE_SAFE_CONTENT_TYPES)


class ReceiptPictureView(View):
    """The picture a receipt's page draws of it, for a file that is an image
    an <img> shows; anything else is not found here.

    Not audited: the browser asks for it on every view of the page, which is
    the read, and HQ records no read of a record's page. Taking the file away
    is the download, which is recorded.
    """

    def get(self, _request, pk: int):
        receipt = get_object_or_404(Receipt, pk=pk)
        content_type = served_type(receipt.content_type)
        if content_type not in PICTURE_CONTENT_TYPES or (path := stored_file(receipt)) is None:
            raise Http404("This receipt has no picture to show.")
        return _file_response(receipt, path, content_type, inline=True)
