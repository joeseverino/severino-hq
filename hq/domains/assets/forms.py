from django import forms

from .models import Asset


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = [
            "item_name",
            "slug",
            "vendor",
            "category",
            "purchase_date",
            "total_cost",
            "business_use_percentage",
            "payment_method",
            "serial_number",
            "warranty_date",
            "status",
            "notes",
            "related_projects",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "purchase_date": forms.DateInput(attrs={"type": "date"}),
            "warranty_date": forms.DateInput(attrs={"type": "date"}),
            "slug": forms.TextInput(
                attrs={"placeholder": "Leave blank to use the name"}
            ),
            "related_projects": forms.SelectMultiple(attrs={"size": 6}),
        }
        help_texts = {
            "business_use_percentage": "0 to 100. Used to estimate the deductible amount.",
        }
