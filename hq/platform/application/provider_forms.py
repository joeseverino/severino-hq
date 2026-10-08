"""A provider's own model is its form.

``control_plane.providers`` already declares each provider once, as a pydantic
model, and four things are derived from that declaration: the JSON Schema the
API publishes, the contract the controller is handed, the validation every
write passes through, and this form. Field types, choices, bounds, defaults and which
fields are optional all come from the model, and a provider added to that tuple
gets a working create-and-edit page with nothing written here.

Two rules keep this honest:

- **Rendering only.** The form decides what the inputs look like. It does not
  decide what is valid: ``clean`` hands the assembled spec back to
  ``validate_spec`` and reports whatever pydantic says. A second implementation
  of the rules would be a second answer to the same question, and the two would
  drift on the day someone tightened one of them.
- **No provider is named.** Nothing below knows what a proxy host is. A field is
  built from its annotation, so the day a provider declares a new one it is
  rendered rather than dropped.
"""

import types
import typing
from typing import Any, ClassVar, override

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

from hq.domains.control_plane.provider_spec import NameContext
from hq.domains.control_plane.providers import PROVIDERS, validate_spec

from .forms import LinesField
from .plugins import _import

# Constraint attributes carried by annotated-types objects in a pydantic field's
# metadata. Read by name rather than by isinstance so a constraint class this
# does not import still contributes what it has.
_CONSTRAINTS = ("min_length", "max_length", "ge", "le", "pattern")


class NameListWidget(forms.Widget):
    """One input per name, with a row to add and a control to remove.

    A textarea holds a list the way a paragraph holds a shopping list: the
    items are there, but nothing about it says where one ends, and editing the
    middle of eight lines is a text-editing exercise rather than a choice.
    A name is the thing being edited, so it gets a field of its own.

    Rows post under the same name and are read back with `getlist`, so the
    field receives an actual list and no parsing rules live in two places.
    Without scripting the existing rows still edit and the spare row still
    adds: only the extra add/remove convenience needs JavaScript.
    """

    @override
    def value_from_datadict(self, data, files, name):
        if hasattr(data, "getlist"):
            return [item for item in data.getlist(name) if str(item).strip()]
        return data.get(name)

    @override
    def format_value(self, value):
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        if value in (None, ""):
            return []
        return [line.strip() for line in str(value).splitlines() if line.strip()]

    # ``{value: note}`` for values HQ can see for itself. A machine's addresses
    # are the case this exists for: half of them are the only record there is
    # (nothing reports a printer's address) and half repeat a reading from the
    # tailnet. Presented identically, the field invites somebody to correct HQ
    # about something HQ is watching, and gives no way to tell which is which.
    notes: ClassVar[dict[str, str]] = {}

    def _row(self, name: str, item: str):
        """One value, editable unless HQ is the one that found it.

        A value carrying a note is a value a sweep reports, so HQ holds it
        whether or not this field does, and removing it would mean deleting a
        fact HQ does not own.

        Submitted as a hidden input rather than left out, so a save keeps what
        it did not ask about instead of quietly dropping it.
        """

        from django.utils.html import format_html

        note = self.notes.get(item, "")
        if note:
            return format_html(
                '<div class="name-list-row name-list-row-observed">'
                '<input type="hidden" name="{}" value="{}">'
                '<span class="name-list-observed">{}</span>'
                '<span class="name-list-note">{}</span>'
                "</div>",
                name,
                item,
                item,
                note,
            )
        return format_html(
            '<div class="name-list-row">'
            '<input type="text" name="{}" value="{}" spellcheck="false"'
            ' autocapitalize="off" autocorrect="off">'
            '<button type="button" class="btn ghost" data-name-list-remove'
            ' aria-label="Remove {}">Remove</button>'
            "</div>",
            name,
            item,
            item,
        )

    @override
    def render(self, name, value, attrs=None, renderer=None):
        from django.utils.html import format_html, format_html_join
        from django.utils.safestring import mark_safe

        # What HQ found first, what only this field records underneath it. The
        # observed one is the address the machine actually answers at on the
        # network everything reaches it over; the typed ones are the exceptions
        # nothing reports. Sorted so read-only rows never sit between inputs
        # and the blank row for adding one stays with the rest.
        values = sorted(self.format_value(value), key=lambda item: item not in self.notes)
        rows = format_html_join("", "{}", ((self._row(name, item),) for item in values))
        # Always one empty row, so adding a name needs no script and no
        # thinking about where the cursor goes.
        blank = format_html(
            '<div class="name-list-row">'
            '<input type="text" name="{}" value="" spellcheck="false"'
            ' autocapitalize="off" autocorrect="off">'
            '<button type="button" class="btn ghost" data-name-list-remove'
            ' aria-label="Remove">Remove</button>'
            "</div>",
            name,
        )
        return mark_safe(
            format_html(
                '<div class="name-list" data-name-list>{}{}'
                '<button type="button" class="btn name-list-add" data-name-list-add>'
                "Add another</button></div>",
                rows,
                blank,
            )
        )


class NameList(LinesField):
    """A list of names, one field per name."""

    widget = NameListWidget

    @override
    def to_python(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        # Commas as well as newlines: the value is usually arriving from
        # somewhere that comma-separates it, and silently keeping
        # "a.example.com, b.example.com" as one name would be a bad surprise.
        text = (value or "").replace(",", "\n")
        return [line.strip() for line in text.splitlines() if line.strip()]

    @override
    def validate(self, value: list[str]) -> None:
        if self.required and not value:
            raise ValidationError(self.error_messages["required"], code="required")


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
        label="Keep this the way HQ sets it",
        help_text=(
            "Switched off, the controller leaves it alone. Switching it off does "
            "not remove anything that is already live."
        ),
    )


class ProviderSpecForm(forms.Form):
    """Rendered from a provider model, and validated by that same model."""

    provider_kind = ""

    advanced_names: tuple[str, ...] = ()

    # The model's own fields, so the form can tell a default from an answer.
    provider_fields: ClassVar[dict] = {}

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
        start renewing is not part of that question.

        Shown rather than hidden, because a default is only a good answer until
        it is not, and a field that has been set comes out from behind the
        disclosure.
        """

        return [field for field in self if self._is_routine(field)]

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

    A provider matches its own records by hostname, never by HQ's key:
    AdGuard finds the rewrite whose ``domain`` equals the spec's, NPM the host
    whose ``domain_names`` match. Changing one renames the record at the
    provider on the next pass, so the form warns that the old name stops
    resolving.

    Read from ``seed`` rather than declared again, because seed already states
    exactly which fields a hostname decides. Only its keys are used, which is
    why a sentinel is safe to pass.

    A covering provider is excluded. A certificate is not found at its provider
    by a name it carries: it is a lineage HQ issues and re-issues, and editing
    which names it covers is exactly how that is done. Warning that the change
    "renames the record and the old name stops resolving" would describe a
    provider that does not work that way, about an edit that is the point.
    """

    provider = PROVIDERS[kind]
    if provider.covers or provider.seed is None:
        return ()
    return tuple(provider.seed(NameContext(hostname="hostname.invalid")))


def spec_form_class(
    kind: str,
    *,
    lock_identity: bool = False,
    context: NameContext | None = None,
) -> type[ProviderSpecForm]:
    """Build the form for one provider kind.

    ``lock_identity`` is for editing an existing record: see
    ``identity_fields`` for why those cannot be changed in place.

    Not cached. Building it is a dict comprehension over a handful of fields,
    and a cache keyed on kind is wrong the moment a test registers a provider,
    which is exactly how the plug-and-play property is proved.
    """

    provider = PROVIDERS[kind]
    context = context or NameContext()
    fields = {name: _field_for(field) for name, field in provider.spec_type.model_fields.items()}
    for name, options in _live_choices(provider, context).items():
        if name not in fields:
            continue
        original = fields[name]
        # Offered rather than typed. An empty list means the thing this field
        # has to name does not exist yet, and the field says so instead of
        # presenting an empty menu that cannot be satisfied.
        many = isinstance(original, NameList)
        field_class = forms.MultipleChoiceField if many else forms.ChoiceField
        fields[name] = field_class(
            choices=options,
            required=original.required,
            label=original.label,
            widget=forms.CheckboxSelectMultiple if many else None,
            help_text=original.help_text
            or ("" if options else "Nothing to choose yet. None have been described to HQ."),
        )
    for name, effect in provider.change_effects:
        if name in fields:
            fields[name].change_effect = effect

    # Which of the values already in a list field HQ can see for itself. Same
    # late resolution and same swallow as the live choices above: a form that
    # will not render because an annotation could not be looked up is the least
    # useful moment to fail.
    for name, notes in _live_notes(provider).items():
        field = fields.get(name)
        if field is not None and isinstance(field.widget, NameListWidget):
            field.widget.notes = notes

    if lock_identity:
        for name in identity_fields(kind):
            if name not in fields:
                continue
            # Editable. The controller is handed what the provider was
            # last seen holding, so it finds the existing record by its old name
            # and updates that one in place: a real rename rather than a
            # second record beside the first. The warning stays because the
            # change reaches a live name on the next pass.
            rename_warning = (
                "Changing this renames the live record within about a minute. The old name stops resolving."
            )
            fields[name].help_text = rename_warning
    return type(
        f"{provider.spec_type.__name__}Form",
        (ProviderSpecForm,),
        {
            "provider_kind": kind,
            "advanced_names": provider.advanced_fields,
            "provider_fields": dict(provider.spec_type.model_fields),
            **fields,
        },
    )


def _optional_inner(annotation: Any) -> Any:
    """``int | None`` is a union, not an int.

    An optional integer must render as an integer input that submits nothing
    when blank. Rendered as text, an empty submission sends "", which a model
    that accepts an integer or None rejects.
    """

    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        named = [argument for argument in typing.get_args(annotation) if argument is not type(None)]
        if len(named) == 1:
            return named[0]
    return annotation


def _field_for(field: Any) -> forms.Field:
    """One pydantic field, as the input that best collects it."""

    annotation = _optional_inner(field.annotation)
    origin = typing.get_origin(annotation)
    limits = _limits(field.metadata)
    required = field.is_required()
    options: dict[str, Any] = {"required": required}
    # ``field.default`` is a sentinel for a field declared with default_factory,
    # and rendering it put the literal string "PydanticUndefined" in the box.
    # get_default runs the factory, which is the actual default.
    default = field.get_default(call_default_factory=True) if not required else None
    if default not in (None, ""):
        options["initial"] = default
    if field.description:
        options["help_text"] = field.description
    # The model's own title, so a field is labelled by the question it asks
    # rather than by the variable that holds the answer. Django would otherwise
    # prettify the attribute name (`topology_ref` as "Topology ref").
    if field.title:
        options["label"] = field.title

    if origin is typing.Literal:
        return forms.ChoiceField(
            choices=[(value, value) for value in typing.get_args(annotation)],
            **options,
        )
    if origin is list:
        return NameList(**options)
    if annotation is bool:
        # A checkbox is never "required": unchecked is a valid answer, and
        # Django reads required=True on a BooleanField as "must be ticked".
        return forms.BooleanField(**{**options, "required": False})
    if annotation is int:
        return forms.IntegerField(min_value=limits.get("ge"), max_value=limits.get("le"), **options)
    return forms.CharField(
        min_length=limits.get("min_length"),
        max_length=limits.get("max_length"),
        validators=([RegexValidator(limits["pattern"])] if limits.get("pattern") else []),
        # A string the model puts no ceiling on is one that can be long, and a
        # long value in a one-line box is unreadable and unusable: an access
        # policy or a compose file arrived as three thousand characters scrolling
        # past a slot two inches wide. Where a length is declared, the model is
        # saying it is short, and a single line is right.
        widget=None
        if limits.get("max_length")
        else forms.Textarea(attrs={"rows": 18, "spellcheck": "false", "class": "code"}),
        **options,
    )


def _live_choices(provider: Any, context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Options a provider says come from live data, resolved late.

    A failure here must not take the form down: the page is how an operator
    fixes things, and refusing to render it because a lookup failed is the
    least useful moment to fail. An empty list renders as "nothing to choose
    yet", which is both true and actionable.
    """

    if not provider.choices:
        return {}
    try:
        return _import(provider.choices)(context) or {}
    except Exception:  # noqa: BLE001 - a broken lookup must not hide the form
        return {}


def _live_notes(provider: Any) -> dict[str, dict[str, str]]:
    """``{field: {value: note}}`` for values a provider says HQ observes."""

    if not getattr(provider, "notes", ""):
        return {}
    try:
        return _import(provider.notes)() or {}
    except Exception:  # noqa: BLE001 - a broken lookup must not hide the form
        return {}


def _limits(metadata: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for constraint in metadata or ():
        for attribute in _CONSTRAINTS:
            value = getattr(constraint, attribute, None)
            if value is not None:
                found[attribute] = value
    return found


def _reported(exc: Exception) -> tuple[tuple[str, str], ...]:
    """Pydantic's failures, paired with the field each belongs to."""

    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return (("", str(exc)),)
    reported = []
    for item in errors():
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
