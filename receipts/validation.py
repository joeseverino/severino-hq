"""Canonical receipt-file policy shared by forms and application services."""

from django.core.exceptions import ValidationError

ALLOWED_RECEIPT_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/gif",
    "image/tiff",
    "text/plain",
}
MAX_RECEIPT_BYTES = 15 * 1024 * 1024


def validate_receipt_file(upload) -> None:
    if upload.size > MAX_RECEIPT_BYTES:
        raise ValidationError(
            f"Receipt is too large ({upload.size} bytes). "
            f"Max: {MAX_RECEIPT_BYTES} bytes."
        )
    content_type = getattr(upload, "content_type", "") or ""
    if content_type and content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
        raise ValidationError(f"Unsupported receipt file type: {content_type}.")
