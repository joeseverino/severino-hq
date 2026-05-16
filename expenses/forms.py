from django import forms

from .models import Expense


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = [
            "date",
            "vendor",
            "item",
            "category",
            "total_cost",
            "business_use_percentage",
            "payment_method",
            "business_purpose",
            "notes",
            "related_project",
            "related_asset",
            "related_content",
            "related_documentation",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "business_purpose": forms.TextInput(),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_business_use_percentage(self):
        v = int(self.cleaned_data.get("business_use_percentage") or 0)
        if not 0 <= v <= 100:
            raise forms.ValidationError("Must be between 0 and 100.")
        return v
