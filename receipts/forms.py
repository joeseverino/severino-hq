from django import forms

from .models import Receipt


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

MAX_RECEIPT_BYTES = 15 * 1024 * 1024  # 15 MB


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
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > MAX_RECEIPT_BYTES:
            raise forms.ValidationError(
                f"Receipt is too large ({f.size} bytes). "
                f"Max: {MAX_RECEIPT_BYTES} bytes."
            )
        content_type = getattr(f, "content_type", "") or ""
        if content_type and content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
            raise forms.ValidationError(
                f"Unsupported file type: {content_type}. "
                "Upload a PDF, image, or plain text."
            )
        return f
