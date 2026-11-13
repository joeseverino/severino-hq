"""The DNS tool's two lookups, each a form that asks rather than writes, the
one question that starts publishing a service, and the two policies a
domain's mail page publishes.

Each lookup field's name is the one its capability's command takes, which
``application.toolkit`` names for the tab and the view reads from the query
string, so a lookup's address is a link anyone can build.
"""

from collections.abc import Mapping
from typing import ClassVar

from django import forms
from django.utils.html import format_html

from hq.platform.application.asks import APPLIES_SHORTLY
from hq.platform.application.forms import ChoiceCards, SuffixedInput
from hq.platform.application.lookup import LOOK_UP_ADDRESS, LOOK_UP_NAME
from hq.platform.application.mail_policy import (
    DMARC_TAGS,
    SPF_DEFAULTS,
    SPF_LOOKUP_LIMIT,
    PolicyTag,
    SpfPolicy,
    SpfTerm,
    compose_dmarc,
    compose_spf,
    parse_spf,
)
from hq.platform.application.provider_forms import NameList
from hq.platform.application.ui import counted
from hq.platform.core.templatetags.value_tags import breakable

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


class MailPolicyForm(forms.Form):
    """One of the mail page's policies, each posting only its own record.

    Both stand on one page, so each names its own ids, and ``section`` says
    which was sent.
    """

    # The section the page posts it as, the record it rewrites, and its verb.
    SECTION: ClassVar[str]
    record: ClassVar[str]
    verb: ClassVar[str]

    section = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, auto_id=f"{self.SECTION}-%s", **kwargs)
        self.fields["section"].initial = self.SECTION

    @property
    def note(self) -> str:
        return f"Saving rewrites the {self.record} record. {APPLIES_SHORTLY}"

    def published(self) -> str:
        """The record the cleaned form publishes as."""

        raise NotImplementedError


class MailSendersForm(MailPolicyForm):
    """Who may send as a domain (SPF): its senders, one rule a row, and what
    happens to every sender it does not list."""

    SECTION = "spf"
    record = "SPF"
    verb = "Save senders"

    rule = NameList(label="Allowed senders", required=False)
    default = forms.ChoiceField(
        label="All other senders",
        choices=[(choice.value, choice.label) for choice in SPF_DEFAULTS],
        widget=ChoiceCards(described={choice.value: choice.consequence for choice in SPF_DEFAULTS}),
    )

    def __init__(self, *args, policy: SpfPolicy, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rule"].initial = [term.rule for term in policy.senders]
        self.fields["rule"].help_text = format_html(
            "Usually <code>include:</code> plus a mail service that sends for you. Each one costs a DNS lookup."
            " Receivers ignore SPF past {}. This record uses {} of {}.",
            breakable(counted(SPF_LOOKUP_LIMIT, "lookup")),
            breakable(policy.lookups),
            breakable(SPF_LOOKUP_LIMIT),
        )
        # The qualifier on the record's `all` term decides everyone unlisted.
        alls = [term.qualifier for term in policy.terms if term.mechanism == "all"]
        self.fields["default"].initial = alls[-1] if alls else "-"

    def published(self) -> str:
        senders = parse_spf(" ".join([compose_spf(()), *self.cleaned_data["rule"]])).terms
        return compose_spf((*senders, SpfTerm(self.cleaned_data["default"], "all", "")))


def _dmarc_field(tag: PolicyTag) -> forms.Field:
    """The field one tag of ``DMARC_TAGS`` is set through, in its words."""

    said = {"label": tag.label, "help_text": tag.help, "required": tag.required}
    if tag.kind == "choice":
        return forms.ChoiceField(
            choices=[(choice.value, choice.label) for choice in tag.choices],
            widget=ChoiceCards(described={choice.value: choice.consequence for choice in tag.choices}),
            **said,
        )
    if tag.kind == "percent":
        return forms.IntegerField(
            min_value=0, max_value=100, widget=SuffixedInput(suffix="%", attrs={"inputmode": "numeric"}), **said
        )
    return forms.CharField(widget=forms.TextInput(attrs={"spellcheck": "false", "autocapitalize": "off"}), **said)


class MailFailuresForm(MailPolicyForm):
    """What receivers do with mail that fails the checks (DMARC), a field for
    each tag ``DMARC_TAGS`` declares."""

    SECTION = "dmarc"
    record = "DMARC"
    verb = "Save policy"

    def __init__(self, *args, tags: Mapping[str, str], **kwargs):
        super().__init__(*args, **kwargs)
        self.record_tags = dict(tags)
        for tag in DMARC_TAGS:
            self.fields[tag.id] = _dmarc_field(tag)
            # A tag the record leaves out is still in force at its default,
            # so the field holds that rather than nothing.
            self.fields[tag.id].initial = tags.get(tag.id) or tag.default

    def published(self) -> str:
        # Unknown tags survive: the record belongs to the operator, and an
        # editor that drops what it does not model deletes policy silently.
        given = {tag.id: self.cleaned_data[tag.id] for tag in DMARC_TAGS}
        return compose_dmarc(
            {**self.record_tags, **{key: "" if value is None else str(value) for key, value in given.items()}}
        )
