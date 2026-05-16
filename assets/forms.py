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
                attrs={"placeholder": "leave blank to auto-generate"}
            ),
            "related_projects": forms.SelectMultiple(attrs={"size": 6}),
        }

    def clean_business_use_percentage(self):
        v = int(self.cleaned_data.get("business_use_percentage") or 0)
        if not 0 <= v <= 100:
            raise forms.ValidationError("Must be between 0 and 100.")
        return v
