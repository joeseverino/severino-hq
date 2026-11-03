"""The receipts resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field

from hq.platform.application import read_models, receipts
from hq.platform.application.answer_fields import UpdatedAt
from hq.platform.application.describe import Describe, listed
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.record_fields import Notes
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

from .models import Receipt
from .validation import FILE_TYPE_LABELS


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
    amount: Annotated[str, Describe("The amount on the receipt, a decimal string with two places such as `12.50`.")]
    notes: Notes
    related_expense: receipts.ReceiptExpense
    related_asset: receipts.ReceiptAsset
    uploaded_at: Annotated[str, Describe("When the file was uploaded, an ISO 8601 date-time.")]
    updated_at: UpdatedAt


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "receipts",
            "Receipts",
            "Receipt metadata without file contents, storage paths, or URLs.",
            Capability.READ,
            read_models.list_receipts,
            ReceiptQuery,
            search=SearchDefinition(
                "receipts",
                Receipt,
                "pk",
                ("original_filename", "vendor", "notes"),
                label="Receipts",
            ),
            web_route="receipts:list",
            row_type=ReceiptRow,
        ),
    )
