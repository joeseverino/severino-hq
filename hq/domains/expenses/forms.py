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
        labels = {
            "related_project": "Project",
            "related_asset": "Asset",
            "related_content": "Writeup or page",
            "related_documentation": "Document",
        }
        help_texts = {
            "business_use_percentage": "0 to 100. Used to estimate the deductible amount.",
        }
