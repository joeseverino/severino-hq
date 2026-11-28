"""The receipts' file commands and resource, declared on its domain in application/domains.py."""

from typing import Annotated, Literal, TypedDict

from django.db.models import Q

from hq.domains.expenses.models import Expense
from hq.platform.application import receipts
from hq.platform.application.answer_fields import PageUrl, UpdatedAt
from hq.platform.application.contracts import Effect
from hq.platform.application.describe import Describe, listed
from hq.platform.application.domains import records_of
from hq.platform.application.integration_specs import (
    CapabilitySpec,
    DetailRead,
    ListRead,
    ListSort,
    ListToggle,
    ResourceSpec,
)
from hq.platform.application.labels import adding
from hq.platform.application.record_fields import Notes, RecordId
from hq.platform.application.records import KEPT
from hq.platform.application.resources import BoundedQuery, SearchText
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

from .models import Receipt
from .validation import FILE_TYPE_LABELS

_FILE_NOTES = (
    "Refuse base64 text longer than the size limit allows, before decoding it.",
    "Refuse a file over the limit, or of a type a receipt cannot be.",
    "Record the file's name, size and SHA-256 digest; its bytes are kept only as the receipt's file.",
)


def capabilities() -> tuple[CapabilitySpec, ...]:
    # A receipt's file arrives only through an operator in the browser or at
    # the host (security.HELD_ONLY_AT), though a token may read the schema.
    authority = (records_of("receipts").write, Capability.UPLOAD_RECEIPT)
    return (
        CapabilitySpec(
            receipts.UPLOAD,
            "Keep a receipt's file, out of reach of any public address, with its details.",
            Effect.REMOTE_WRITE,
            authority,
            receipts.ReceiptUploadCommand,
            receipts.upload_receipt,
            subject_resource="receipts",
            execution_notes=_FILE_NOTES,
            label=adding("receipt"),
        ),
        CapabilitySpec(
            receipts.REPLACE,
            f"Replace a receipt's file, and change its details with it. {KEPT}",
            Effect.REMOTE_WRITE,
            authority,
            receipts.ReceiptUploadCommand,
            receipts.replace_receipt,
            "integer",
            "receipts",
            target_label="Receipt",
            target_help="The receipt whose file to replace.",
            execution_notes=_FILE_NOTES,
            label="Replace a receipt's file",
        ),
    )


class ReceiptQuery(BoundedQuery):
    query: SearchText = None


def _unlinked() -> tuple[tuple[int, ...], tuple[int, ...]]:
    """A receipt matched to nothing, kept, and one that proves an expense, left out."""

    proof = Expense.objects.create(date="2030-01-02", vendor="Example Host", item="Hosting")
    kept = Receipt.objects.create(vendor="Example Host")
    return (kept.pk,), (Receipt.objects.create(vendor="Example Host", related_expense=proof).pk,)


UNLINKED = ListToggle(
    "unlinked",
    "No expense or asset",
    "True for only the receipts matched to no expense and no asset.",
    Q(related_expense__isnull=True, related_asset__isnull=True),
    _unlinked,
)


# Each order the list is read in, the first where none is asked.
SORTS = (
    *ListSort.both("-uploaded_at", "Recently uploaded", "Least recently uploaded"),
    *ListSort.both("vendor", "Vendor"),
    *ListSort.both("date", "Oldest receipt date", "Newest receipt date"),
    *ListSort.both("amount", "Lowest amount", "Highest amount"),
    *ListSort.both("original_filename", "File"),
)


class ReceiptRow(TypedDict):
    """One receipt's metadata; never its file, path or address."""

    id: Annotated[int, Describe("The receipt's id, which its page names it by.")]
    original_filename: Annotated[str, Describe("The file's name as it was uploaded; empty when it had none.")]
    content_type: Annotated[str, Describe(ended(f"The file's media type: {listed(FILE_TYPE_LABELS)}"))]
    size_bytes: Annotated[int, Describe("How big the file is, in bytes.")]
    vendor: Annotated[str, Describe("Who was paid, as the receipt names them; empty when not recorded.")]
    date: Annotated[str | None, Describe("The day on the receipt, an ISO 8601 date; null when not recorded.")]
    amount: Annotated[
        str, Describe("The amount on the receipt, a decimal string with two places.", examples=("12.50",))
    ]
    notes: Notes
    label: Annotated[str, Describe("What the receipt is called wherever it is named: who it is from, else its file.")]
    related_expense: receipts.ReceiptExpense
    expense_item: Annotated[str, Describe("What the expense it proves paid for; empty for none.")]
    related_asset: receipts.ReceiptAsset
    asset_name: Annotated[str, Describe("The name of the asset it is for; empty for none.")]
    uploaded_at: Annotated[str, Describe("When the file was uploaded, an ISO 8601 date-time.")]
    updated_at: UpdatedAt


class ReceiptRecord(ReceiptRow):
    """One receipt with whether HQ holds its file, and the name and day of the expense it proves."""

    url: PageUrl
    file: Annotated[
        Literal["stored", "missing", "none"],
        Describe(
            "Whether HQ holds its file: `stored`, `missing` when HQ's storage no longer has it, or `none` for no file."
        ),
    ]
    file_type_label: Annotated[str, Describe("The file's type in words; empty for one HQ does not accept.")]
    expense_label: Annotated[str, Describe("What the expense it proves is called; empty for none.")]
    expense_date: Annotated[str | None, Describe("That expense's day, an ISO 8601 date; null for none.")]


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "receipts",
            "Receipts",
            "Receipt metadata without file contents, storage paths, or URLs.",
            Capability.READ,
            listing=ListRead(receipts.list_receipts, ReceiptQuery, ReceiptRow, sorts=SORTS, toggles=(UNLINKED,)),
            detail=DetailRead(receipts.get_receipt, ReceiptRecord),
            identifier="id",
            identifier_type=RecordId,
            search=SearchDefinition.of(
                Receipt,
                "pk",
                ("original_filename", "vendor", "notes"),
                timestamp_field="date",
            ),
            web_route="receipts:list",
        ),
    )
