"""Canonical receipt-file policy shared by forms and application services."""

import math

from django.core.files.uploadedfile import UploadedFile

from hq.platform.application.labels import human_bytes
from hq.platform.application.record_errors import InputError

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
# The longest base64 text a file within the limit encodes to, so a longer one
# is refused before anything is decoded.
MAX_RECEIPT_BASE64_CHARS = 4 * math.ceil(MAX_RECEIPT_BYTES / 3)

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


def is_new_upload(file: object) -> bool:
    """Whether a receipt's file is one just uploaded, so checked and stored.

    Every request.FILES entry is an UploadedFile. An edit without one cleans to
    the stored FieldFile, which was held to these rules when it arrived and
    carries no content type of its own.
    """

    return isinstance(file, UploadedFile)


def validate_receipt_file(upload: UploadedFile) -> None:
    """Refuse a file the receipt rules do not admit, in the words every adapter shows."""

    if upload.size > MAX_RECEIPT_BYTES:
        raise InputError(f"This file is {human_bytes(upload.size)}. The limit is {human_bytes(MAX_RECEIPT_BYTES)}.")
    # An unstated type is a rejected upload, not a trusted one: the allowlist
    # has to be a gate every upload passes through, not one it can decline.
    content_type = (upload.content_type or "").strip().lower()
    if not content_type:
        raise InputError("This file does not say what type it is. Upload a PDF or an image.")
    if content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
        # Said without the type, which the uploader chose: the text reaches
        # every adapter as the host's own words.
        raise InputError("A receipt cannot be this type of file. Upload a PDF or an image.")
