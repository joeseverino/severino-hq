from django import forms

from .models import Project


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            "name",
            "slug",
            "category",
            "status",
            "description",
            "technologies_used",
            "repository_url",
            "public_url",
            "deployment_notes",
            "security_notes",
            "notes",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "deployment_notes": forms.Textarea(attrs={"rows": 4}),
            "security_notes": forms.Textarea(attrs={"rows": 4}),
            "notes": forms.Textarea(attrs={"rows": 4}),
            "slug": forms.TextInput(
                attrs={"placeholder": "leave blank to auto-generate"}
            ),
        }

    def clean_slug(self):
        return (self.cleaned_data.get("slug") or "").strip()
