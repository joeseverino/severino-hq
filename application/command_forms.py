"""Django forms derived from the capability registry's JSON Schemas."""

from __future__ import annotations

import re
import secrets
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

from .capabilities import CapabilitySpec, command_schema
from .command_targets import CommandTargetOption


_EXECUTION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class PrimitiveListField(forms.Field):
    """One JSON primitive per line, preserving the schema's item type."""

    widget = forms.Textarea(attrs={"rows": 4, "spellcheck": "false", "class": "code"})

    def __init__(self, *args, item_type: str = "string", **kwargs):
        self.item_type = item_type
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return "\n".join(str(item) for item in value)
        return value

    def to_python(self, value):
        lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        if self.item_type == "integer":
            try:
                return [int(line) for line in lines]
            except ValueError as exc:
                raise ValidationError("Enter one whole number per line.") from exc
        if self.item_type == "number":
            try:
                return [float(line) for line in lines]
            except ValueError as exc:
                raise ValidationError("Enter one number per line.") from exc
        if self.item_type == "boolean":
            values = {"true": True, "false": False}
            try:
                return [values[line.casefold()] for line in lines]
            except KeyError as exc:
                raise ValidationError("Enter true or false, one per line.") from exc
        return lines


def _one_type(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten the common nullable JSON-Schema shape for native controls."""

    choices = schema.get("anyOf")
    if not isinstance(choices, list):
        return schema
    concrete = [item for item in choices if item.get("type") != "null"]
    if len(concrete) != 1:
        return schema
    return {**schema, **concrete[0], "anyOf": choices}


def _field_for_schema(
    name: str, schema: dict[str, Any], *, required: bool
) -> forms.Field:
    effective = _one_type(schema)
    kind = effective.get("type")
    options: dict[str, Any] = {
        "required": required,
        "label": schema.get("title") or name.replace("_", " ").title(),
        "help_text": schema.get("description", ""),
    }
    if "default" in schema and schema["default"] is not None:
        options["initial"] = schema["default"]

    choices = effective.get("enum")
    if choices:
        return forms.ChoiceField(
            choices=[(value, str(value)) for value in choices], **options
        )
    if kind == "boolean":
        return forms.BooleanField(**{**options, "required": False})
    if kind == "integer":
        return forms.IntegerField(
            min_value=effective.get("minimum"),
            max_value=effective.get("maximum"),
            **options,
        )
    if kind == "number":
        return forms.DecimalField(
            min_value=effective.get("minimum"),
            max_value=effective.get("maximum"),
            **options,
        )
    if kind == "array":
        item_schema = effective.get("items", {})
        item_type = item_schema.get("type")
        if item_type in {"string", "integer", "number", "boolean"}:
            return PrimitiveListField(item_type=item_type, **options)
        return forms.JSONField(
            widget=forms.Textarea(
                attrs={"rows": 10, "spellcheck": "false", "class": "code"}
            ),
            **options,
        )
    if kind == "object":
        return forms.JSONField(
            widget=forms.Textarea(
                attrs={"rows": 10, "spellcheck": "false", "class": "code"}
            ),
            **options,
        )
    if effective.get("format") == "date":
        return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **options)
    if effective.get("format") == "date-time":
        return forms.DateTimeField(
            widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), **options
        )

    validators = []
    if effective.get("pattern"):
        validators.append(RegexValidator(re.compile(effective["pattern"])))
    return forms.CharField(
        min_length=effective.get("minLength"),
        max_length=effective.get("maxLength"),
        validators=validators,
        **options,
    )


class CapabilityCommandForm(forms.Form):
    """Presentation from JSON Schema; execution still validates canonically."""

    payload_names: tuple[str, ...] = ()
    payload_schema: dict[str, Any] = {}
    effect = "read"

    @property
    def primary_fields(self):
        return tuple(
            field
            for field in self.visible_fields()
            if field.name not in {"__expected_updated_at", "__confirm_effect", "next"}
        )

    @property
    def advanced_fields(self):
        return tuple(
            field
            for field in self.visible_fields()
            if field.name == "__expected_updated_at"
        )

    @property
    def confirmation_fields(self):
        return tuple(
            field
            for field in self.visible_fields()
            if field.name == "__confirm_effect"
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "idempotency_key" in self.fields and not self.is_bound:
            self.fields["idempotency_key"].initial = f"command:{secrets.token_urlsafe(18)}"
        if not self.is_bound or not hasattr(self.data, "getlist"):
            return
        allowed = set(self.fields) | {"csrfmiddlewaretoken"}
        unknown = sorted(set(self.data) - allowed)
        repeated = sorted(
            name
            for name in self.data
            if name in allowed
            and name != "csrfmiddlewaretoken"
            and len(self.data.getlist(name)) > 1
        )
        self.submission_errors = (
            *((f"Unknown field: {name}." for name in unknown)),
            *((f"Repeated field: {name}." for name in repeated)),
        )

    def clean(self):
        cleaned = super().clean()
        for message in getattr(self, "submission_errors", ()):
            self.add_error(None, message)
        if self.errors:
            return cleaned
        self.command_payload = {}
        for name in self.payload_names:
            value = cleaned.get(name)
            field_schema = self.payload_schema[name]
            if value in (None, "") and "default" in field_schema:
                continue
            if value is None and name not in self.payload_schema.get("required", ()):
                continue
            self.command_payload[name] = value
        return cleaned


def command_form_class(
    spec: CapabilitySpec,
    *,
    target_options: tuple[CommandTargetOption, ...] | None = None,
) -> type[CapabilityCommandForm]:
    """Build the complete browser form from one canonical capability spec."""

    schema = command_schema(spec.command_type)
    properties = schema.get("properties", {})
    required = frozenset(schema.get("required", ()))
    fields = {
        name: _field_for_schema(name, field, required=name in required)
        for name, field in properties.items()
    }
    if "idempotency_key" in fields:
        fields["idempotency_key"].widget = forms.HiddenInput()
    if spec.target_kind:
        options = {
            "label": spec.target_label or "Target",
            "help_text": spec.target_help or "The existing object this command acts on.",
        }
        if target_options is None:
            target_field = (
                forms.IntegerField
                if spec.target_kind == "integer"
                else forms.CharField
            )
            fields["__target"] = target_field(**options)
        else:
            empty_label = (
                f"Select {(spec.target_label or 'target').lower()}…"
                if target_options
                else "No eligible targets are currently available"
            )
            fields["__target"] = forms.ChoiceField(
                choices=(("", empty_label),)
                + tuple((item.value, item.label) for item in target_options),
                **options,
            )
        fields = {"__target": fields.pop("__target"), **fields}
        fields["__expected_updated_at"] = forms.CharField(
            required=False,
            label="Expected updated at",
            help_text="Optional optimistic-concurrency timestamp from the current record.",
        )
    fields["__execution_key"] = forms.CharField(
        max_length=128,
        validators=[
            RegexValidator(
                _EXECUTION_KEY,
                "The execution key must contain only URL-safe characters.",
            )
        ],
        widget=forms.HiddenInput(),
    )
    fields["next"] = forms.CharField(required=False, widget=forms.HiddenInput())
    if spec.effect in {"infrastructure_change", "destructive"}:
        fields["__confirm_effect"] = forms.BooleanField(
            label=(
                "I understand this command may change external infrastructure."
                if spec.effect == "infrastructure_change"
                else "I understand this command is destructive."
            )
        )
    return type(
        f"{schema.get('title', 'Capability')}WebForm",
        (CapabilityCommandForm,),
        {
            "payload_names": tuple(properties),
            "payload_schema": {**properties, "required": tuple(required)},
            "effect": spec.effect,
            **fields,
        },
    )
