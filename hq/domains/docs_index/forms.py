from django import forms

from hq.platform.application.forms import FileField

# Several thousand doc records are a few megabytes.
MAX_MANIFEST_BYTES = 10 * 1024 * 1024


class ManifestImportForm(forms.Form):
    manifest_file = FileField(
        max_bytes=MAX_MANIFEST_BYTES,
        label="Manifest JSON",
        # The importer accepts one shape of file; the picker should offer that
        # shape. Both spellings, because a vault export written by a script is
        # as likely to arrive typed `application/json` as named `.json`.
        widget=forms.ClearableFileInput(attrs={"accept": ".json,application/json"}),
        help_text="JSON array of doc records from the vault export.",
    )
    update_existing = forms.BooleanField(
        required=False,
        initial=True,
        label="Update existing records (match on doc_id)",
    )
