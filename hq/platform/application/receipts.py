"""Receipt metadata and protected-upload application services."""

import base64
import hashlib
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal
from typing import Annotated, Any, TypedDict

from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile
from django.db import transaction
from pydantic import Field

from hq.domains.assets.models import Asset
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.domains.receipts.validation import MAX_RECEIPT_BASE64_CHARS, validate_receipt_file
from hq.platform.core.audit import audit_operation, record_event
from hq.platform.core.models import AuditLog
lazy from hq.domains.receipts.specs import ReceiptRow

from .command_fields import Describe, Names, Upload
from .integration_specs import Kept, keeps
from .projection import iso
from .record_errors import InputError, RecordNotFound
from .record_fields import Notes
from .record_saves import Held, as_sent, found, held, written
from .security import Principal
lazy from .record_answers import ReceiptSaved, ReceiptUploaded

UPLOAD = "receipt.upload"
REPLACE = "receipt.replace"
# The record capability application.records derives from the domain's declaration.
UPDATE = "receipt.update"

_RECEIPT = Held("That receipt was not found.", "That receipt changed after it was read.")


# The records a receipt is matched to, as its command writes them and its row reads them back.
type ReceiptExpense = Annotated[
    int | None,
    Names("expenses", key="id"),
    Describe("The expense this receipt proves.", api="Its id; null for none."),
]
type ReceiptAsset = Annotated[
    str | None, Names("assets"), Describe("The asset this receipt is for.", api="Its slug; null for none.")
]


@dataclass(frozen=True, slots=True)
class ReceiptMetadataCommand:
    vendor: Annotated[str, Describe("Who issued the receipt.")] = ""
    date: Annotated[Date | None, Describe("The date on the receipt.")] = None
    amount: Annotated[Decimal, Describe("The amount it shows.")] = Decimal("0.00")
    notes: Notes = ""
    related_expense: ReceiptExpense = None
    related_asset: ReceiptAsset = None


@dataclass(frozen=True, slots=True)
class ReceiptFile:
    """A receipt's file as JSON carries it. Its bytes are stored as the
    receipt's file and nowhere else: the audit trail and a response name it by
    its name, size and digest."""

    name: Annotated[str, Field(min_length=1)]
    content_type: str
    # Bounded before it is decoded, so a file over the limit is refused
    # without its bytes ever being held.
    content: Annotated[str, Field(max_length=MAX_RECEIPT_BASE64_CHARS, description="The file's bytes, base64-encoded.")]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptUploadCommand(ReceiptMetadataCommand):
    file: Annotated[ReceiptFile, Upload(), Describe("The receipt's file.")]


def _stored(current_id: int) -> dict[str, Any]:
    receipt = found(Receipt, _RECEIPT, {"pk": current_id})
    return as_sent(receipt, ReceiptMetadataCommand, {"related_expense": "id", "related_asset": "slug"})


# What changing a receipt keeps; a replacement always sends its file.
_KEPT = Kept(_stored)
_REPLACED = Kept(_stored, always_sent=frozenset({"file"}))


def serialize_receipt(receipt: Receipt) -> ReceiptRow:
    return {
        "id": receipt.id,
        "original_filename": receipt.original_filename,
        "content_type": receipt.content_type,
        "size_bytes": receipt.size_bytes,
        "vendor": receipt.vendor,
        "date": iso(receipt.date),
        "amount": str(receipt.amount),
        "notes": receipt.notes,
        "related_expense": receipt.related_expense_id,
        "related_asset": receipt.related_asset.slug if receipt.related_asset else None,
        "uploaded_at": receipt.uploaded_at.isoformat(),
        "updated_at": receipt.updated_at.isoformat(),
    }


def _relations(command: ReceiptMetadataCommand):
    try:
        expense = Expense.objects.get(pk=command.related_expense) if command.related_expense is not None else None
        asset = Asset.objects.get(slug=command.related_asset) if command.related_asset else None
    except (Expense.DoesNotExist, Asset.DoesNotExist) as exc:
        raise RecordNotFound("A related expense or asset was not found.") from exc
    return expense, asset


def _decoded(file: ReceiptFile) -> tuple[UploadedFile, str]:
    """The file as an upload the receipt rules hold, and its SHA-256 digest."""

    try:
        content = base64.b64decode(file.content, validate=True)
    except ValueError as exc:
        raise InputError("The file's content is not base64.") from exc
    upload = SimpleUploadedFile(file.name, content, content_type=file.content_type)
    validate_receipt_file(upload)
    return upload, hashlib.sha256(content).hexdigest()


def _save(
    command: ReceiptMetadataCommand,
    *,
    current_id: int,
    expected_updated_at: str | None,
    sent: frozenset[str] | None,
    upload: UploadedFile | None = None,
) -> Receipt:
    receipt = held(Receipt, _RECEIPT, {"pk": current_id}, expected_updated_at)
    values = written(command, sent)
    expense, asset = _relations(command)
    if upload is not None:
        _hold_file(receipt, upload)
    for field in ("vendor", "date", "amount", "notes"):
        if field in values:
            setattr(receipt, field, values[field])
    if "related_expense" in values:
        receipt.related_expense = expense
    if "related_asset" in values:
        receipt.related_asset = asset
    receipt.full_clean(exclude=("file",))
    receipt.save()
    return receipt


def _hold_file(receipt: Receipt, upload: UploadedFile) -> None:
    receipt.file = upload
    receipt.original_filename = (upload.name or "")[:255]
    receipt.content_type = upload.content_type or ""
    receipt.size_bytes = upload.size or 0


def _uploaded(receipt: Receipt, sha256: str) -> StoredReceiptFile:
    """Record the file's arrival by name, size and digest, and answer the same."""

    told = {"name": receipt.original_filename, "size_bytes": receipt.size_bytes, "sha256": sha256}
    record_event(
        action=AuditLog.Action.UPLOADED,
        obj=receipt,
        type_label="Receipt",
        message=f"Receipt uploaded: {receipt.original_filename}",
        metadata={**told, "content_type": receipt.content_type},
    )
    return told


class StoredReceiptFile(TypedDict):
    """The file a receipt holds, as it arrived."""

    name: Annotated[str, Describe("The file's name, as uploaded.")]
    size_bytes: Annotated[int, Describe("The file's size, in bytes.")]
    sha256: Annotated[str, Describe("The file's SHA-256 digest, in hex.")]


@keeps(_KEPT)
@transaction.atomic
def update_receipt(
    command: ReceiptMetadataCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
    sent: frozenset[str] | None = None,
) -> ReceiptSaved:
    with audit_operation(operation=UPDATE, principal=principal):
        receipt = _save(command, current_id=current_id, expected_updated_at=expected_updated_at, sent=sent)
    return {"ok": True, "created": False, "receipt": serialize_receipt(receipt)}


@keeps(_REPLACED)
@transaction.atomic
def replace_receipt(
    command: ReceiptUploadCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
    sent: frozenset[str] | None = None,
) -> ReceiptUploaded:
    """The ``receipt.replace`` capability, which authorizes it."""

    del principal
    upload, sha256 = _decoded(command.file)
    receipt = _save(command, current_id=current_id, expected_updated_at=expected_updated_at, sent=sent, upload=upload)
    return {"ok": True, "created": False, "receipt": serialize_receipt(receipt), "file": _uploaded(receipt, sha256)}


@transaction.atomic
def upload_receipt(command: ReceiptUploadCommand, *, principal: Principal) -> ReceiptUploaded:
    """The ``receipt.upload`` capability, which authorizes it."""

    del principal
    upload, sha256 = _decoded(command.file)
    expense, asset = _relations(command)
    receipt = Receipt(
        vendor=command.vendor,
        date=command.date,
        amount=command.amount,
        notes=command.notes,
        related_expense=expense,
        related_asset=asset,
    )
    _hold_file(receipt, upload)
    receipt.full_clean()
    receipt.save()
    return {"ok": True, "created": True, "receipt": serialize_receipt(receipt), "file": _uploaded(receipt, sha256)}


def file_cleanup(receipt: Receipt):
    """Delete the private file once the receipt's own delete has committed."""

    name = receipt.file.name if receipt.file else ""
    storage = receipt.file.storage if receipt.file else None
    if not name or storage is None:
        return None
    return lambda: storage.delete(name)
