from django import forms

# Several thousand doc records are a few megabytes.
MAX_MANIFEST_BYTES = 10 * 1024 * 1024


class ManifestImportForm(forms.Form):
    manifest_file = forms.FileField(
        label="Manifest JSON",
        # The importer accepts one shape of file; the picker should offer that
        # shape. Both spellings, because a vault export written by a script is
        # as likely to arrive typed `application/json` as named `.json`.
        widget=forms.ClearableFileInput(attrs={"accept": ".json,application/json"}),
        help_text=("JSON array of doc records from the vault export. Same format as the import_docs_manifest command."),
    )
    update_existing = forms.BooleanField(
        required=False,
        initial=True,
        label="Update existing records (match on doc_id)",
    )

    def clean_manifest_file(self):
        manifest = self.cleaned_data["manifest_file"]
        if manifest.size > MAX_MANIFEST_BYTES:
            raise forms.ValidationError(f"A manifest is at most {MAX_MANIFEST_BYTES // (1024 * 1024)} MB.")
        return manifest
