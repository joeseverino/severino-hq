"""Receipt metadata and protected-upload application services."""

import base64
import hashlib
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal
from typing import Annotated, Any

from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile
from django.db import transaction
from pydantic import Field

from hq.domains.assets.models import Asset
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.domains.receipts.validation import MAX_RECEIPT_BASE64_CHARS, validate_receipt_file
from hq.platform.core.audit import operation_context, record_event
from hq.platform.core.models import AuditLog

from .command_fields import Describe, Names
from .projection import iso
from .record_errors import InputError, RecordNotFound
from .record_saves import Held, held
from .security import Principal

UPLOAD = "receipt.upload"
REPLACE = "receipt.replace"
# The record capability application.records derives from the domain's declaration.
UPDATE = "receipt.update"

_RECEIPT = Held("That receipt was not found.", "That receipt changed after it was read.")


@dataclass(frozen=True, slots=True)
class ReceiptMetadataCommand:
    vendor: Annotated[str, Describe("Who issued the receipt.")] = ""
    date: Annotated[Date | None, Describe("The date on the receipt.")] = None
    amount: Annotated[Decimal, Describe("The amount it shows.")] = Decimal("0.00")
    notes: Annotated[str, Describe("Anything else worth keeping.")] = ""
    related_expense: Annotated[
        int | None, Names("expenses", key="id"), Describe("The expense this receipt proves.")
    ] = None
    related_asset: Annotated[str | None, Names("assets"), Describe("The asset this receipt is for.")] = None


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
    file: Annotated[ReceiptFile, Describe("The receipt's file.")]


def serialize_receipt(receipt: Receipt) -> dict[str, Any]:
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
        "uploaded_at": iso(receipt.uploaded_at),
        "updated_at": iso(receipt.updated_at),
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
    upload: UploadedFile | None = None,
) -> Receipt:
    receipt = held(Receipt, _RECEIPT, {"pk": current_id}, expected_updated_at)
    expense, asset = _relations(command)
    if upload is not None:
        _hold_file(receipt, upload)
    for field in ("vendor", "date", "amount", "notes"):
        setattr(receipt, field, getattr(command, field))
    receipt.related_expense = expense
    receipt.related_asset = asset
    receipt.full_clean(exclude=("file",))
    receipt.save()
    return receipt


def _hold_file(receipt: Receipt, upload: UploadedFile) -> None:
    receipt.file = upload
    receipt.original_filename = (upload.name or "")[:255]
    receipt.content_type = upload.content_type or ""
    receipt.size_bytes = upload.size or 0


def _uploaded(receipt: Receipt, sha256: str) -> dict[str, Any]:
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


@transaction.atomic
def update_receipt(
    command: ReceiptMetadataCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    with operation_context(interface=principal.interface, actor=principal.actor, operation=UPDATE):
        receipt = _save(command, current_id=current_id, expected_updated_at=expected_updated_at)
    return {"ok": True, "created": False, "receipt": serialize_receipt(receipt)}


@transaction.atomic
def replace_receipt(
    command: ReceiptUploadCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """The ``receipt.replace`` capability, which authorizes it."""

    del principal
    upload, sha256 = _decoded(command.file)
    receipt = _save(command, current_id=current_id, expected_updated_at=expected_updated_at, upload=upload)
    return {"ok": True, "created": False, "receipt": serialize_receipt(receipt), "file": _uploaded(receipt, sha256)}


@transaction.atomic
def upload_receipt(command: ReceiptUploadCommand, *, principal: Principal) -> dict[str, Any]:
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


def receipt_payload(data: dict[str, Any], upload: UploadedFile | None = None) -> dict[str, Any]:
    """A receipt form's cleaned data as the receipt capabilities take it, with
    the file the form brought, if it brought one."""

    payload: dict[str, Any] = {
        "vendor": data["vendor"],
        "date": iso(data["date"]),
        "amount": str(data["amount"]),
        "notes": data["notes"],
        "related_expense": data["related_expense"].id if data["related_expense"] else None,
        "related_asset": data["related_asset"].slug if data["related_asset"] else None,
    }
    if upload is not None:
        upload.seek(0)
        payload["file"] = {
            "name": upload.name or "",
            "content_type": upload.content_type or "",
            "content": base64.b64encode(upload.read()).decode("ascii"),
        }
    return payload
