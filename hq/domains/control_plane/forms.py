"""The DNS tool's two lookups, each a form that asks rather than writes, and
the one question that starts publishing a service.

Each lookup field's name is the one its capability's command takes, which
``application.toolkit`` names for the tab and the view reads from the query
string, so a lookup's address is a link anyone can build.
"""

from typing import ClassVar

from django import forms

from hq.platform.application.lookup import LOOK_UP_ADDRESS, LOOK_UP_NAME

from .names import normalized_hostname

# A value typed to be looked up, never prose or a remembered entry.
_TYPED = {"spellcheck": "false", "autocomplete": "off"}


class LookupForm(forms.Form):
    """One lookup on the tools page: the tab it answers on, and what it asks."""

    # Both lookups stand on one page, so each names its own ids.
    ids: ClassVar[str]

    tab = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, auto_id=f"{self.ids}-%s", **kwargs)


class NameLookupForm(LookupForm):
    ids = "name-lookup"

    name = forms.CharField(
        label="Hostname",
        required=False,
        widget=forms.TextInput(attrs={**_TYPED, "placeholder": "example.com"}),
    )


class AddressLookupForm(LookupForm):
    ids = "address-lookup"

    address = forms.CharField(
        label="IP address",
        required=False,
        widget=forms.TextInput(attrs={**_TYPED, "placeholder": "1.1.1.1"}),
    )


LOOKUP_FORMS: dict[str, type[LookupForm]] = {LOOK_UP_NAME: NameLookupForm, LOOK_UP_ADDRESS: AddressLookupForm}


class ServiceStartForm(forms.Form):
    """The name a service answers at: the one thing HQ cannot work out."""

    hostname = forms.CharField(
        label="Hostname",
        help_text="The name people will use.",
        widget=forms.TextInput(attrs={"spellcheck": "false", "autofocus": True, "placeholder": "app.example.com"}),
    )

    def clean_hostname(self) -> str:
        hostname = normalized_hostname(self.cleaned_data["hostname"])
        if not hostname or " " in hostname or "." not in hostname:
            raise forms.ValidationError("Enter a hostname, like app.example.com.")
        return hostname
