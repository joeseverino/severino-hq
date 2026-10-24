from pathlib import Path

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.urls import reverse

from .models import Receipt
from .validation import (
    ALLOWED_RECEIPT_CONTENT_TYPES,
    MAX_RECEIPT_BYTES,
    validate_receipt_file,
)

# What the picker offers, taken from the policy that decides rather than
# restated beside it. A phone camera roll narrows on this attribute, so the two
# disagreeing means being offered a file the server then refuses after upload.
RECEIPT_ACCEPT = ",".join(sorted(ALLOWED_RECEIPT_CONTENT_TYPES))
RECEIPT_SIZE_HINT = f"PDF or image · up to {MAX_RECEIPT_BYTES // (1024 * 1024)} MB"


class _StoredReceipt:
    """The file a receipt already holds, named and linked as HQ serves it."""

    def __init__(self, name: str, url: str) -> None:
        self.name, self.url = name, url

    def __str__(self) -> str:
        return self.name


class ReceiptFileInput(forms.ClearableFileInput):
    """Links "Currently" to the protected file view; MEDIA_URL is never served."""

    stored: _StoredReceipt | None = None

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        if self.stored and context["widget"]["is_initial"]:
            context["widget"]["value"] = self.stored
        return context


class ReceiptUploadForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        receipt = self.instance
        if receipt.pk and receipt.file:
            self.fields["file"].widget.stored = _StoredReceipt(
                receipt.original_filename or Path(receipt.file.name).name,
                reverse("receipts:file", args=[receipt.pk]),
            )

    class Meta:
        model = Receipt
        fields = [
            "file",
            "vendor",
            "date",
            "amount",
            "related_expense",
            "related_asset",
            "notes",
        ]
        widgets = {
            "file": ReceiptFileInput(attrs={"accept": RECEIPT_ACCEPT}),
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        # The model's help text says where the file goes, which the page
        # already says above the form. What the operator cannot find out
        # anywhere else is what will be refused, so the field says that.
        help_texts = {"file": RECEIPT_SIZE_HINT}
        labels = {"related_expense": "Expense", "related_asset": "Asset"}

    def clean_file(self):
        f = self.cleaned_data["file"]
        # An edit without an upload cleans to the stored file, which was held
        # to these rules when it arrived and carries no content type of its own.
        if isinstance(f, UploadedFile):
            validate_receipt_file(f)
        return f
