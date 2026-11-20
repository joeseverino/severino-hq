"""A provider's own model is its form.

``control_plane.providers`` declares each provider once, as a pydantic model,
and the API's JSON Schema, the controller's contract, write validation and this
form all derive from it, so a new provider gets a create-and-edit page with
nothing written here. The form is drawn from the published schema as every
command's is (``command_forms.schema_fields``), only renders (``clean`` asks
``validate_spec``) and names no provider.
"""

import logging
from typing import Any, ClassVar, override

from django import forms
from pydantic import ValidationError as SpecError

from hq.domains.control_plane.provider_spec import NameContext, ProviderSpec
from hq.domains.control_plane.providers import PROVIDERS, validate_spec

from .command_forms import holding, schema_fields
from .command_targets import CommandTargetOption
from .describe import meanings
from .forms import ListWidget, ReadAt
from .plugins import import_reference

logger = logging.getLogger("severino.providers")


class ResourceIdentityForm(forms.Form):
    """What HQ calls a resource, and whether it reconciles.

    Separate from the spec form because it is the one part of a resource that
    is not the provider's business. Two forms, each with one job, beats a
    generated form that has to remember which of its own fields are not spec.
    """

    # No identifier field. The identifier is the string in this page's address
    # and in every operation and audit entry recorded against the resource, so
    # it must not move. It is derived from the name when the resource is
    # created and never asked about again, not even as a disabled input; the
    # readout above the form shows it.
    enabled = forms.BooleanField(
        required=False,
        initial=True,
        label="Managed by HQ",
        help_text="Turn off to stop HQ changing it. Nothing is removed.",
    )
    expected_updated_at = ReadAt()


class ProviderSpecForm(forms.Form):
    """Rendered from a provider model, and validated by that same model."""

    provider_kind = ""

    advanced_names: tuple[str, ...] = ()

    # The model's own fields, so the form can tell a default from an answer.
    provider_fields: ClassVar[dict] = {}

    # The fields whose options come from live readings: each holds its value as an option.
    live_names: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in self.live_names:
            field = self.fields[name]
            holding(field, self.initial.get(name, field.initial))

    def _is_routine(self, field) -> bool:
        """Whether this field is still just a default nobody chose.

        A knob is routine while it holds the answer the model would have given
        anyway. Once somebody has set it, it is part of what this resource *is*
        and belongs with the question rather than behind a disclosure.
        """

        if field.name not in self.advanced_names:
            return False
        model_field = self.provider_fields.get(field.name)
        default = model_field.get_default(call_default_factory=True) if model_field is not None else None
        value = self.initial.get(field.name, default)
        if value in (None, "", [], ()):
            return True
        return value == default

    @property
    def primary(self):
        """The question being asked, plus any knob somebody has answered."""

        return [field for field in self if not self._is_routine(field)]

    @property
    def advanced(self):
        """Routine tuning, one disclosure away.

        A certificate asks which certificate; how many days before expiry to
        start renewing is not part of that question. Shown rather than hidden,
        since a default is only a good answer until it is not, and a field that
        has been set comes out from behind the disclosure.
        """

        return [field for field in self if self._is_routine(field)]

    # The form decides what the inputs look like, never what is valid: a
    # second implementation of the model's rules would drift from it the day
    # one of them tightened.
    @override
    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        if self.errors:
            return cleaned
        # An omitted optional field is left out entirely rather than sent as
        # None, so the model applies its own default. Sending None asks pydantic
        # to accept a value the annotation forbids, and restating the default
        # here would put it in two places that could disagree.
        payload = {name: cleaned[name] for name in self.fields if cleaned.get(name) is not None}
        try:
            self.spec = validate_spec(self.provider_kind, payload)
        except (KeyError, TypeError, ValueError) as exc:
            # Pydantic's own messages, addressed to the field that caused them
            # where it names one. Restating them here would mean maintaining a
            # second vocabulary for the same failures.
            for location, message in _reported(exc):
                self.add_error(location if location in self.fields else None, message)
        return cleaned


def identity_fields(kind: str) -> tuple[str, ...]:
    """The spec fields that decide which record this is at the provider.

    A provider matches its own records by hostname, never by HQ's key (AdGuard
    a rewrite's ``domain``, NPM a host's ``domain_names``), so changing one
    renames the record at the provider on the next pass, and the form warns
    that the old name stops resolving.
    """

    provider = PROVIDERS[kind]
    # A certificate is not found at its provider by a name it carries: it is a
    # lineage HQ issues and re-issues, and editing which names it covers is how
    # that is done, so the rename warning would describe a provider that does
    # not work that way, about an edit that is the point.
    if provider.covers or provider.seed is None:
        return ()
    # ``seed`` already states which fields a hostname decides. Only its keys
    # are read, which is why a sentinel hostname is safe.
    return tuple(provider.seed(NameContext(hostname="hostname.invalid")))


def spec_form_class(
    kind: str,
    *,
    lock_identity: bool = False,
    context: NameContext | None = None,
) -> type[ProviderSpecForm]:
    """Build the form for one provider kind.

    ``lock_identity`` is for editing an existing record (``identity_fields``
    says why). Not cached: building it is cheap, and a cache keyed on kind is
    wrong the moment a test registers a provider, which is how the plug-and-play
    property is proved.
    """

    provider = PROVIDERS[kind]
    context = context or NameContext()
    schema = provider.schema()
    # A live reading (a ``(value, label)`` pair each) is offered as the records it names.
    live = {
        name: tuple(CommandTargetOption(str(value), str(label)) for value, label in options)
        for name, options in _live(provider, provider.choices, context).items()
        if name in schema.get("properties", {})
    }
    fields = schema_fields(schema, offered=live, said=meanings(provider.spec_type))
    for name, effect in provider.change_effects:
        if name in fields:
            fields[name].change_effect = effect

    # Which of the values already in a list field HQ can see for itself. Same
    # late resolution and same swallow as the live choices above: a form that
    # will not render because an annotation could not be looked up is the least
    # useful moment to fail.
    for name, notes in _live(provider, provider.notes).items():
        field = fields.get(name)
        if field is not None and isinstance(field.widget, ListWidget):
            field.widget.notes = notes

    if lock_identity:
        for name in identity_fields(kind):
            if name not in fields:
                continue
            # Editable. The controller is handed what the provider was
            # last seen holding, so it finds the existing record by its old name
            # and updates that one in place: a real rename rather than a
            # second record beside the first. The warning stays because the
            # change reaches a live name; when is the form's apply note.
            fields[name].help_text = "Changing this renames the live record. The old name stops resolving."
    return type(
        f"{provider.spec_type.__name__}Form",
        (ProviderSpecForm,),
        {
            "provider_kind": kind,
            "advanced_names": provider.advanced_fields,
            "provider_fields": dict(provider.spec_type.model_fields),
            "live_names": tuple(live),
            **fields,
        },
    )


def _live(provider: ProviderSpec, reference: str, *args: Any) -> dict[str, Any]:
    """What a provider's live lookup (its ``choices`` or ``notes``) answers, resolved late.

    A failure here must not take the form down: the page is how an operator
    fixes things, and refusing to render it because a lookup failed is the
    least useful moment to fail. An empty answer renders as "nothing to choose
    from yet", which is both true and actionable, and the failure is logged.
    """

    if not reference:
        return {}
    try:
        return import_reference(reference)(*args) or {}
    except Exception:  # a broken lookup must not hide the form
        logger.exception(
            "The live lookup %s of %s failed.", reference, provider.kind, extra={"event": "providers.lookup.failed"}
        )
        return {}


def _reported(exc: Exception) -> tuple[tuple[str, str], ...]:
    """Pydantic's failures, paired with the field each belongs to."""

    if not isinstance(exc, SpecError):
        return (("", str(exc)),)
    reported = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        reported.append((location, item.get("msg", "Invalid value.")))
    return tuple(reported) or (("", str(exc)),)


class SecretTextarea(forms.Textarea):
    """A textarea that never renders a value back, like a password input.

    A form re-shown after an error would otherwise put the submitted key back
    into the page: into its HTML, the browser's cache and its history.
    """

    @override
    def format_value(self, value):
        return None


class CertificateUploadForm(forms.Form):
    """The two files ``cert-gen`` produced, pasted in.

    Pasted rather than uploaded because that is what the operator already has:
    the runbook ends with opening fullchain.pem and copying it into a web form.
    A file input would be tidier and would mean finding the directory again.
    """

    fullchain = forms.CharField(
        label="Certificate",
        widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}),
        help_text="The contents of fullchain.pem: the certificate and its CA chain.",
    )
    private_key = forms.CharField(
        label="Private key",
        widget=SecretTextarea(attrs={"rows": 8, "spellcheck": "false", "autocomplete": "off"}),
        help_text=(
            "The contents of the .key file. Encrypted before it is stored, and "
            "never shown again or returned by any API."
        ),
    )
