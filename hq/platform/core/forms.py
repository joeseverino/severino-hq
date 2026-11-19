"""The host's own small forms, each drawn by partials/_form.html. Each field
is named for what its command takes, and its view still reads the post."""

from django import forms

# A value typed as itself, never prose or a remembered entry.
_TYPED = {"spellcheck": "false", "autocomplete": "off"}


class DashboardLinksForm(forms.Form):
    """Which outward links the dashboard shows, a box to tick for each, from
    ``outward_links.link_choices``."""

    href = forms.MultipleChoiceField(label="Show", required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, choices, *args, **kwargs):
        super().__init__(*args, auto_id="links-%s", **kwargs)
        self.fields["href"].choices = [(choice["href"], choice["label"]) for choice in choices]
        self.initial["href"] = [choice["href"] for choice in choices if choice["chosen"]]


class OwnDeviceForm(forms.Form):
    """One of the signed-in person's own apps, registered by its client id."""

    client_id = forms.CharField(label="Client id", widget=forms.TextInput(attrs=_TYPED))
    registered = forms.CharField(widget=forms.HiddenInput, initial="1")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, auto_id="device-%s", **kwargs)
