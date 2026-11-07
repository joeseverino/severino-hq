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
            "paid_from",
            "business_purpose",
            "notes",
            "related_project",
            "related_asset",
            "related_content",
            "related_documentation",
            "about",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "business_purpose": forms.TextInput(),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {
            "related_project": "Project",
            "related_asset": "Asset",
            "related_content": "Writeup or page",
            "related_documentation": "Document",
            "paid_from": "Paid from",
            "about": "Also for",
        }
