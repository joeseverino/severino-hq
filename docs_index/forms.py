from django import forms

from .models import DocumentationRecord


class DocumentationRecordForm(forms.ModelForm):
    class Meta:
        model = DocumentationRecord
        fields = [
            "doc_id",
            "title",
            "doc_type",
            "system_service",
            "environment",
            "status",
            "sensitivity",
            "obsidian_path",
            "github_path",
            "external_url",
            "last_reviewed",
            "notes",
            "related_projects",
            "related_assets",
            "related_expenses",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "last_reviewed": forms.DateInput(attrs={"type": "date"}),
            "related_projects": forms.SelectMultiple(attrs={"size": 6}),
            "related_assets": forms.SelectMultiple(attrs={"size": 6}),
            "related_expenses": forms.SelectMultiple(attrs={"size": 6}),
        }
        help_texts = {
            "doc_id": "Stable identifier, e.g. 'rb-adguard-001'.",
            "notes": "Index notes only. Do NOT paste full runbook contents or secrets.",
        }


class ManifestImportForm(forms.Form):
    manifest_file = forms.FileField(
        label="Manifest JSON",
        help_text=(
            "JSON array of documentation records as produced by the Obsidian "
            "vault export script. See docs_index/management/commands/import_docs_manifest.py."
        ),
    )
    update_existing = forms.BooleanField(
        required=False,
        initial=True,
        label="Update existing records (matched by doc_id)",
    )
