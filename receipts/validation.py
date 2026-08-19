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
# What may be handed back to a browser to render in place. Everything else is
# downloaded instead: a receipt is a document to look at, and no format outside
# this set needs to execute in HQ's origin to be read.
INLINE_SAFE_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}

MAX_RECEIPT_BYTES = 15 * 1024 * 1024


def validate_receipt_file(upload) -> None:
    if upload.size > MAX_RECEIPT_BYTES:
        raise ValidationError(
            f"Receipt is too large ({upload.size} bytes). "
            f"Max: {MAX_RECEIPT_BYTES} bytes."
        )
    # An unstated type is a rejected upload, not a trusted one: the allowlist
    # has to be a gate every upload passes through, not one it can decline.
    content_type = (getattr(upload, "content_type", "") or "").strip().lower()
    if not content_type:
        raise ValidationError(
            "The receipt file did not declare a type, so it cannot be accepted."
        )
    if content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
        raise ValidationError(f"Unsupported receipt file type: {content_type}.")
