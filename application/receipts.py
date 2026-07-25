"""Receipt metadata and protected-upload application services."""

from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal
from typing import Any

from django.db import transaction

from assets.models import Asset
from core.audit import operation_context, record_event
from core.models import AuditLog
from expenses.models import Expense
from receipts.models import Receipt
from receipts.validation import validate_receipt_file

from .security import Capability, Principal


class NotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class ReceiptMetadataCommand:
    vendor: str = ""
    date: Date | None = None
    amount: Decimal = Decimal("0.00")
    notes: str = ""
    related_expense: int | None = None
    related_asset: str | None = None


def serialize_receipt(receipt: Receipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "original_filename": receipt.original_filename,
        "content_type": receipt.content_type,
        "size_bytes": receipt.size_bytes,
        "vendor": receipt.vendor,
        "date": receipt.date.isoformat() if receipt.date else None,
        "amount": str(receipt.amount),
        "notes": receipt.notes,
        "related_expense": receipt.related_expense_id,
        "related_asset": receipt.related_asset.slug if receipt.related_asset else None,
        "uploaded_at": receipt.uploaded_at.isoformat(),
        "updated_at": receipt.updated_at.isoformat(),
    }


def _relations(command: ReceiptMetadataCommand):
    try:
        expense = (
            Expense.objects.get(pk=command.related_expense)
            if command.related_expense is not None
            else None
        )
        asset = (
            Asset.objects.get(slug=command.related_asset)
            if command.related_asset
            else None
        )
    except (Expense.DoesNotExist, Asset.DoesNotExist) as exc:
        raise NotFoundError("Related expense or asset was not found.") from exc
    return expense, asset


@transaction.atomic
def update_receipt(
    command: ReceiptMetadataCommand,
    *,
    principal: Principal,
    current_id: int,
    expected_updated_at: str | None = None,
    upload=None,
) -> dict[str, Any]:
    principal.require(Capability.WRITE_RECEIPTS)
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation="receipt.update"
    ):
        try:
            receipt = Receipt.objects.select_for_update().get(pk=current_id)
        except Receipt.DoesNotExist as exc:
            raise NotFoundError(f"Receipt {current_id!r} was not found.") from exc
        if expected_updated_at and receipt.updated_at.isoformat() != expected_updated_at:
            raise ValueError(f"Receipt {current_id!r} changed after it was read.")
        expense, asset = _relations(command)
        if upload is not None and hasattr(upload, "content_type"):
            validate_receipt_file(upload)
            receipt.file = upload
            receipt.original_filename = upload.name[:255]
            receipt.content_type = getattr(upload, "content_type", "") or ""
            receipt.size_bytes = upload.size or 0
        for field in ("vendor", "date", "amount", "notes"):
            setattr(receipt, field, getattr(command, field))
        receipt.related_expense = expense
        receipt.related_asset = asset
        receipt.full_clean(exclude=("file",))
        receipt.save()
    return {"ok": True, "created": False, "receipt": serialize_receipt(receipt)}


@transaction.atomic
def upload_receipt(
    command: ReceiptMetadataCommand,
    upload,
    *,
    principal: Principal,
) -> dict[str, Any]:
    principal.require(Capability.WRITE_RECEIPTS)
    validate_receipt_file(upload)
    expense, asset = _relations(command)
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation="receipt.upload"
    ):
        receipt = Receipt(
            file=upload,
            original_filename=upload.name[:255],
            content_type=getattr(upload, "content_type", "") or "",
            size_bytes=upload.size or 0,
            vendor=command.vendor,
            date=command.date,
            amount=command.amount,
            notes=command.notes,
            related_expense=expense,
            related_asset=asset,
        )
        receipt.full_clean()
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
    return {"ok": True, "created": True, "receipt": serialize_receipt(receipt)}


def receipt_command_from_cleaned_data(data) -> ReceiptMetadataCommand:
    return ReceiptMetadataCommand(
        vendor=data["vendor"],
        date=data["date"],
        amount=data["amount"],
        notes=data["notes"],
        related_expense=(
            data["related_expense"].id if data["related_expense"] else None
        ),
        related_asset=data["related_asset"].slug if data["related_asset"] else None,
    )
