"""The receipts' file commands and resource, declared on its domain in application/domains.py."""

from typing import Annotated, Literal, TypedDict

from pydantic import Field

from hq.platform.application import receipts
from hq.platform.application.answer_fields import UpdatedAt
from hq.platform.application.contracts import Effect
from hq.platform.application.describe import Describe, listed
from hq.platform.application.domains import records_of
from hq.platform.application.integration_specs import CapabilitySpec, ListRead, ResourceSpec
from hq.platform.application.labels import adding
from hq.platform.application.record_fields import Notes, RecordId
from hq.platform.application.records import KEPT
from hq.platform.application.resources import BoundedQuery
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
    unmatched_only: bool = Field(
        default=False, description="True for only the receipts matched to no expense and no asset."
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
    related_expense: receipts.ReceiptExpense
    related_asset: receipts.ReceiptAsset
    uploaded_at: Annotated[str, Describe("When the file was uploaded, an ISO 8601 date-time.")]
    updated_at: UpdatedAt


class ReceiptRecord(ReceiptRow):
    """One receipt with what it is called, whether HQ holds its file, and the names of what it proves."""

    label: Annotated[str, Describe("What the receipt is called wherever it is named: who it is from, else its file.")]
    file: Annotated[
        Literal["stored", "missing", "none"],
        Describe(
            "Whether HQ holds its file: `stored`, `missing` when HQ's storage no longer has it, or `none` for no file."
        ),
    ]
    file_type_label: Annotated[str, Describe("The file's type in words; empty for one HQ does not accept.")]
    expense_label: Annotated[str, Describe("What the expense it proves is called; empty for none.")]
    expense_date: Annotated[str | None, Describe("That expense's day, an ISO 8601 date; null for none.")]
    asset_name: Annotated[str, Describe("The name of the asset it is for; empty for none.")]


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "receipts",
            "Receipts",
            "Receipt metadata without file contents, storage paths, or URLs.",
            Capability.READ,
            listing=ListRead(receipts.list_receipts, ReceiptQuery, ReceiptRow),
            detail_handler=receipts.get_receipt,
            identifier="id",
            identifier_type=RecordId,
            search=SearchDefinition(
                "receipts",
                Receipt,
                "pk",
                ("original_filename", "vendor", "notes"),
                label="Receipts",
            ),
            web_route="receipts:list",
            record_type=ReceiptRecord,
        ),
    )
