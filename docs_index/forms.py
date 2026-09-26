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
            "notes": "Index notes only. No runbook text or secrets.",
        }


class ManifestImportForm(forms.Form):
    manifest_file = forms.FileField(
        label="Manifest JSON",
        # The importer accepts one shape of file; the picker should offer that
        # shape. Both spellings, because a vault export written by a script is
        # as likely to arrive typed `application/json` as named `.json`.
        widget=forms.ClearableFileInput(attrs={"accept": ".json,application/json"}),
        help_text=(
            "JSON array of doc records from the vault export. Same format as "
            "the import_docs_manifest command."
        ),
    )
    update_existing = forms.BooleanField(
        required=False,
        initial=True,
        label="Update existing records (match on doc_id)",
    )
