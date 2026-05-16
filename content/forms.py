from django import forms

from .models import ContentItem


class ContentItemForm(forms.ModelForm):
    class Meta:
        model = ContentItem
        fields = [
            "title",
            "slug",
            "content_type",
            "status",
            "topic",
            "tags",
            "published_url",
            "wordpress_post_id",
            "wordpress_slug",
            "published_at",
            "notes",
            "related_projects",
            "related_assets",
            "related_expenses",
            "related_documentation",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "published_at": forms.DateInput(attrs={"type": "date"}),
            "slug": forms.TextInput(
                attrs={"placeholder": "leave blank to auto-generate"}
            ),
            "related_projects": forms.SelectMultiple(attrs={"size": 6}),
            "related_assets": forms.SelectMultiple(attrs={"size": 6}),
            "related_expenses": forms.SelectMultiple(attrs={"size": 6}),
            "related_documentation": forms.SelectMultiple(attrs={"size": 6}),
        }
