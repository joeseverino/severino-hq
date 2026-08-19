"""A provider's own model is its form.

``control_plane.providers`` already declares each provider once, as a pydantic
model, and three things are derived from that declaration: the JSON Schema the
API publishes, the contract the controller is handed, and the validation every
write passes through. The web had a fourth copy of the same knowledge -- except
it did not, because nobody wrote it, which is why infrastructure could be
created over the API and the MCP but not in HQ.

So the form is derived too. Field types, choices, bounds, defaults and which
fields are optional all come from the model, and a provider added to that tuple
gets a working create-and-edit page with nothing written here.

Two rules keep this honest:

- **Rendering only.** The form decides what the inputs look like. It does not
  decide what is valid -- ``clean`` hands the assembled spec back to
  ``validate_spec`` and reports whatever pydantic says. A second implementation
  of the rules would be a second answer to the same question, and the two would
  drift on the day someone tightened one of them.
- **No provider is named.** Nothing below knows what a proxy host is. A field is
  built from its annotation, so the day a provider declares a new one it is
  rendered rather than dropped.
"""

from __future__ import annotations

import typing
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

from control_plane.providers import PROVIDERS, validate_spec

from .plugins import _import

# Constraint attributes carried by annotated-types objects in a pydantic field's
# metadata. Read by name rather than by isinstance so a constraint class this
# does not import still contributes what it has.
_CONSTRAINTS = ("min_length", "max_length", "ge", "le", "pattern")


class NameList(forms.Field):
    """A list of names, entered one per line.

    A `list[str]` needs a real widget. Rendered as a single comma-joined text
    input, a certificate covering eight names becomes an unreadable line that
    invites a typo in the middle of it, and the field is exactly where a typo
    silently stops matching a hostname.
    """

    widget = forms.Textarea(attrs={"rows": 3, "spellcheck": "false"})

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return "\n".join(str(item) for item in value)
        return value

    def to_python(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        # Commas as well as newlines: the value is usually arriving from
        # somewhere that comma-separates it, and silently keeping
        # "a.example.com, b.example.com" as one name would be a bad surprise.
        text = (value or "").replace(",", "\n")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def validate(self, value: list[str]) -> None:
        if self.required and not value:
            raise ValidationError(self.error_messages["required"], code="required")


class ResourceIdentityForm(forms.Form):
    """What HQ calls a resource, and whether it reconciles.

    Separate from the spec form because it is the one part of a resource that
    is not the provider's business. Two forms, each with one job, beats a
    generated form that has to remember which of its own fields are not spec.
    """

    key = forms.SlugField(
        max_length=180,
        required=False,
        label="Name in HQ",
        help_text=(
            "Optional. Left blank, HQ names it after the hostname. Stable once "
            "set — operations refer to it."
        ),
    )
    enabled = forms.BooleanField(
        required=False,
        initial=True,
        label="Reconcile this resource",
        help_text=(
            "Disabled resources are left alone by the controller. Disabling does "
            "not remove anything already applied at the provider."
        ),
    )


class ProviderSpecForm(forms.Form):
    """Rendered from a provider model, and validated by that same model."""

    provider_kind = ""

    advanced_names: tuple[str, ...] = ()

    @property
    def primary(self):
        """The fields that make up the question being asked."""

        return [field for field in self if field.name not in self.advanced_names]

    @property
    def advanced(self):
        """Routine tuning, one disclosure away.

        A certificate asks which certificate; how many days before expiry to
        start renewing is not part of that question. A proxy host had eight such
        knobs in front of the four that matter.

        Shown rather than hidden, because a default is only a good answer until
        the day it is not.
        """

        return [field for field in self if field.name in self.advanced_names]

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        if self.errors:
            return cleaned
        # An omitted optional field is left out entirely rather than sent as
        # None, so the model applies its own default. Sending None asks pydantic
        # to accept a value the annotation forbids, and restating the default
        # here would put it in two places that could disagree.
        payload = {
            name: cleaned[name]
            for name in self.fields
            if cleaned.get(name) is not None
        }
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

    A provider matches its own records by hostname, never by HQ's key --
    AdGuard finds the rewrite whose ``domain`` equals the spec's, NPM the host
    whose ``domain_names`` match. So changing one of these does not rename
    anything: reconciliation looks for the new name, does not find it, and
    creates it, leaving the old record in place and serving. Neither provider
    has a delete path here, so nothing can clean that up afterwards.

    Read from ``seed`` rather than declared again, because seed already states
    exactly which fields a hostname decides. Only its keys are used, which is
    why a sentinel is safe to pass.
    """

    provider = PROVIDERS[kind]
    return tuple(provider.seed("hostname.invalid")) if provider.seed else ()


def spec_form_class(
    kind: str, *, lock_identity: bool = False
) -> type[ProviderSpecForm]:
    """Build the form for one provider kind.

    ``lock_identity`` is for editing an existing record: see
    ``identity_fields`` for why those cannot be changed in place.

    Not cached. Building it is a dict comprehension over a handful of fields,
    and a cache keyed on kind is wrong the moment a test registers a provider --
    which is exactly how the plug-and-play property is proved.
    """

    provider = PROVIDERS[kind]
    fields = {
        name: _field_for(field)
        for name, field in provider.spec_type.model_fields.items()
    }
    for name, options in _live_choices(provider).items():
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
            or (
                ""
                if options
                else "Nothing to choose yet — none have been described to HQ."
            ),
        )
    if lock_identity:
        for name in identity_fields(kind):
            if name not in fields:
                continue
            # Django's ``disabled`` ignores submitted data and keeps the initial
            # value, so this holds against a crafted POST as well as a stray
            # click -- which matters, because the damage is silent and permanent.
            fields[name].disabled = True
            fields[name].help_text = (
                "Fixed after creation. The provider identifies this record by "
                "this value, so changing it would create a second record and "
                "leave the current one in place."
            )
    return type(
        f"{provider.spec_type.__name__}Form",
        (ProviderSpecForm,),
        {
            "provider_kind": kind,
            "advanced_names": provider.advanced_fields,
            **fields,
        },
    )


def _field_for(field: Any) -> forms.Field:
    """One pydantic field, as the input that best collects it."""

    annotation = field.annotation
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
    # prettify the attribute name, which turned `topology_ref` into
    # "Topology ref" -- an accurate name for the field and no help at all.
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
        return forms.IntegerField(
            min_value=limits.get("ge"), max_value=limits.get("le"), **options
        )
    return forms.CharField(
        min_length=limits.get("min_length"),
        max_length=limits.get("max_length"),
        validators=(
            [RegexValidator(limits["pattern"])] if limits.get("pattern") else []
        ),
        **options,
    )


def _live_choices(provider: Any) -> dict[str, tuple[tuple[str, str], ...]]:
    """Options a provider says come from live data, resolved late.

    A failure here must not take the form down: the page is how an operator
    fixes things, and refusing to render it because a lookup failed is the
    least useful moment to fail. An empty list renders as "nothing to choose
    yet", which is both true and actionable.
    """

    if not provider.choices:
        return {}
    try:
        return _import(provider.choices)() or {}
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
