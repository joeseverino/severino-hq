from django import forms

from .models import Receipt
from .validation import validate_receipt_file


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
        validate_receipt_file(f)
        return f
