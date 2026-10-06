"""Canonical receipt-file policy shared by forms and application services."""

from django.core.exceptions import ValidationError

from hq.platform.application.labels import human_bytes

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

# What a stored file type is called on a page.
FILE_TYPE_LABELS = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG image",
    "image/png": "PNG image",
    "image/webp": "WebP image",
    "image/heic": "HEIC image",
    "image/heif": "HEIF image",
    "image/gif": "GIF image",
    "image/tiff": "TIFF image",
    "text/plain": "Text file",
}


def validate_receipt_file(upload) -> None:
    if upload.size > MAX_RECEIPT_BYTES:
        raise ValidationError(
            f"This file is {human_bytes(upload.size)}. The limit is {human_bytes(MAX_RECEIPT_BYTES)}."
        )
    # An unstated type is a rejected upload, not a trusted one: the allowlist
    # has to be a gate every upload passes through, not one it can decline.
    content_type = (getattr(upload, "content_type", "") or "").strip().lower()
    if not content_type:
        raise ValidationError("This file does not say what type it is. Upload a PDF or an image.")
    if content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
        raise ValidationError(f"A receipt cannot be this type of file ({content_type}). Upload a PDF or an image.")
