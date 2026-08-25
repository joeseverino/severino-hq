from django import forms

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


class ReceiptUploadForm(forms.ModelForm):
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
            "file": forms.ClearableFileInput(attrs={"accept": RECEIPT_ACCEPT}),
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        # The model's help text says where the file goes, which the page
        # already says above the form. What the operator cannot find out
        # anywhere else is what will be refused, so the field says that.
        help_texts = {"file": RECEIPT_SIZE_HINT}

    def clean_file(self):
        f = self.cleaned_data["file"]
        validate_receipt_file(f)
        return f
