"""
Storage for receipt uploads.

Receipts are stored OUTSIDE the application code on disk (SEVERINO_MEDIA_ROOT)
and are NOT served by the web server's static handler. They are reachable only
through the auth-protected ``receipts:file`` view, which streams the file.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def receipt_upload_path(instance, filename: str) -> str:
    """Date-bucketed, content-addressed path for a receipt file.

    The original filename is preserved as the suffix so downloads remain
    recognizable, but the basename is randomized so receipts cannot be
    enumerated via predictable URLs.
    """

    suffix = Path(filename).suffix.lower()
    if len(suffix) > 12:  # guard against absurd extensions
        suffix = suffix[:12]
    date = instance.date or instance.uploaded_at.date() if instance.uploaded_at else None
    if date is None:
        from django.utils import timezone

        date = timezone.localdate()
    return f"receipts/{date:%Y/%m}/{uuid.uuid4().hex}{suffix}"


class PrivateReceiptStorage(FileSystemStorage):
    """File storage rooted at SEVERINO_MEDIA_ROOT.

    ``base_url`` is intentionally None so calling ``.url`` raises — there is
    no public URL for these files.
    """

    def __init__(self) -> None:
        super().__init__(
            location=str(settings.MEDIA_ROOT),
            base_url=None,
            file_permissions_mode=0o640,
            directory_permissions_mode=0o750,
        )
