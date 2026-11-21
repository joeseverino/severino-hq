"""Django forms derived from the capability registry's JSON Schemas."""

from collections.abc import Mapping
from typing import Any, ClassVar, override

from django import forms
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinLengthValidator, MinValueValidator
from django.utils.datastructures import MultiValueDict

from .capabilities import CapabilitySpec
from .command_fields import ACCEPTS, among_now, for_agents, is_upload, named_records, referred
from .command_targets import CommandTargetOption
from .contracts import Effect
from .describe import meanings
from .forms import ListField, ReadAt, UploadField
from .integration_specs import IDEMPOTENCY_FIELD, capability_schema, command_schema
from .json_schemas import LOCAL, fields as whole_fields, labeled_choices
from .labels import field_label
from .references import keeping_held, leave_off_unoffered
from .ui import ended
from .web_writes import RETRY_KEY_FIELD

# What a choice of records offers when there are none to choose.
NOTHING_TO_CHOOSE = "Nothing to choose from yet"


def _one_type(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten the common nullable JSON-Schema shape for native controls, and
    a decimal, sent as a number or its text, to the number."""

    choices = schema.get("anyOf")
    if not isinstance(choices, list):
        return schema
    concrete = [item for item in choices if item.get("type") != "null"]
    numbers = [item for item in concrete if item.get("type") == "number"]
    if len(concrete) == 2 and len(numbers) == 1 and {item.get("type") for item in concrete} == {"number", "string"}:
        concrete = numbers
    if len(concrete) != 1:
        return schema
    return {**schema, **concrete[0], "anyOf": choices}


class JSONText(forms.JSONField):
    """A value of no declared shape, written as JSON; blank while it holds none."""

    widget = forms.Textarea(attrs={"rows": 10, "spellcheck": "false", "class": "code"})

    @override
    def prepare_value(self, value):
        return "" if value is None else super().prepare_value(value)


def _is_closed(schema: dict[str, Any]) -> bool:
    """Whether an object's schema names every field it takes, so each is drawn
    as a field of its own rather than as JSON. A file is an object too, picked
    whole."""

    effective = _one_type(schema)
    return (
        effective.get("type") == "object"
        and isinstance(effective.get("properties"), dict)
        and not effective.get("additionalProperties")
        and not is_upload(schema)
    )


# What a field left blank holds: a closed object's, when every field it holds was.
_BLANK = (None, "", [], {})

# Between a closed object's name and each of its fields' names in the form,
# so ``parameters.cache`` is the ``cache`` the ``parameters`` object holds.
_INSIDE = "."


def _inside(
    name: str, schema: dict[str, Any], definitions: Mapping[str, Any], *, required: bool, nested: dict
) -> dict[str, forms.Field]:
    """The fields a closed object is drawn as, each named under ``name``, and
    each object among them in turn; ``nested`` gains what each object holds."""

    effective = _one_type(schema)
    held = frozenset(effective.get("required", ())) if required else frozenset()
    children = whole_fields({"properties": effective["properties"], "$defs": definitions})
    nested[name] = tuple((child, child in held) for child in children)
    drawn: dict[str, forms.Field] = {}
    for child, field in children.items():
        path = f"{name}{_INSIDE}{child}"
        if _is_closed(field):
            drawn |= _inside(path, field, definitions, required=child in held, nested=nested)
        else:
            drawn[path] = _field_for_schema(child, field, required=child in held)
    return drawn


def _record_choice(
    effective: dict[str, Any], offered: tuple[CommandTargetOption, ...], options: dict[str, Any]
) -> forms.Field:
    """A field that names records, as a choice of those records.

    The choices are the records the caller may read, so a value outside them
    is refused here, before the command runs.
    """

    if effective.get("type") == "array":
        coerce = int if effective.get("items", {}).get("type") == "integer" else str
        if not offered:
            options["help_text"] = " ".join(part for part in (options["help_text"], ended(NOTHING_TO_CHOOSE)) if part)
        # Every choice in sight at once, a box to each, as the form draws a
        # group of choices (forms.css): a list box hides those past its edge.
        return forms.TypedMultipleChoiceField(
            choices=grouped_choices(offered), coerce=coerce, widget=forms.CheckboxSelectMultiple, **options
        )
    empty = "Choose one…" if options["required"] else "None"
    if not offered:
        empty = NOTHING_TO_CHOOSE
    nullable = any(arm.get("type") == "null" for arm in effective.get("anyOf", ()))
    return forms.TypedChoiceField(
        choices=(("", empty), *grouped_choices(offered)),
        coerce=int if effective.get("type") == "integer" else str,
        # None chooses no record where the field may name none.
        empty_value=None if nullable else "",
        **options,
    )


def _field_for_schema(
    name: str,
    schema: dict[str, Any],
    *,
    required: bool,
    offered: tuple[CommandTargetOption, ...] | None = None,
    meaning: str | None = None,
) -> forms.Field:
    effective = _one_type(schema)
    kind = effective.get("type")
    options: dict[str, Any] = {
        "required": required,
        "label": field_label(name, schema.get("title")),
        # What the field means, without the part only a client reads (``describe``).
        "help_text": schema.get("description", "") if meaning is None else meaning,
    }
    if "default" in schema and schema["default"] is not None:
        options["initial"] = schema["default"]
    if offered is not None:
        return _record_choice(effective, offered, options)
    if is_upload(schema):
        accepts = ",".join(schema.get(ACCEPTS, ()))
        return UploadField(widget=forms.ClearableFileInput(attrs={"accept": accepts} if accepts else {}), **options)

    choices = labeled_choices(effective)
    if choices:
        return forms.ChoiceField(choices=choices, **options)
    if kind == "boolean":
        return forms.BooleanField(**{**options, "required": False})
    if kind == "integer":
        return _drawn_only(
            forms.IntegerField(min_value=effective.get("minimum"), max_value=effective.get("maximum"), **options)
        )
    if kind == "number":
        # A decimal's places are its step, so the control counts in cents.
        step = effective.get("multipleOf")
        return _drawn_only(
            forms.DecimalField(
                min_value=effective.get("minimum"),
                max_value=effective.get("maximum"),
                widget=forms.NumberInput(attrs={"step": str(step)} if step else {}),
                **options,
            )
        )
    if kind == "array":
        item_schema = effective.get("items", {})
        item_type = item_schema.get("type")
        if offers := labeled_choices(item_schema):
            return forms.TypedMultipleChoiceField(
                choices=offers,
                coerce=int if item_type == "integer" else str,
                widget=forms.CheckboxSelectMultiple,
                **options,
            )
        if item_type in {"string", "integer", "number", "boolean"}:
            return ListField(item_type=item_type, **options)
        return JSONText(**options)
    if kind == "object":
        return JSONText(**options)
    if effective.get("format") == "date":
        return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **options)
    if effective.get("format") == "date-time":
        return forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), **options)
    if effective.get("format") == "partial-time":
        return forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}), **options)

    if effective.get("format") == "textarea" or effective.get("contentMediaType"):
        # A document of a media type (a policy, a PEM file) is typed as itself.
        typed = _TYPED if effective.get("contentMediaType") else {}
        # A value written but never read back, a key, is never drawn back either.
        textarea = SecretTextarea if effective.get("writeOnly") else forms.Textarea
        widget: forms.Widget = textarea(attrs={"rows": 4, **typed})
    else:
        # A value of a declared shape is typed as itself, never prose or a
        # remembered entry, and the schema's first example shows the shape.
        typed = _TYPED if effective.get("pattern") or effective.get("format") else {}
        examples = effective.get("examples") or ()
        widget = forms.TextInput(attrs={**typed, **({"placeholder": str(examples[0])} if examples else {})})
    return _drawn_only(
        forms.CharField(
            widget=widget,
            min_length=effective.get("minLength"),
            max_length=effective.get("maxLength"),
            **options,
        )
    )


_TYPED = {"spellcheck": "false", "autocomplete": "off"}


class SecretTextarea(forms.Textarea):
    """A textarea that never renders a value back, like a password input.

    A form re-shown after an error would otherwise put the submitted key back
    into the page: into its HTML, the browser's cache and its history.
    """

    @override
    def format_value(self, value):
        return None


def _drawn_only[F: forms.Field](field: F) -> F:
    """``field`` with the schema's bounds as its control's attributes alone.

    The command refuses a value outside them as it is validated, in the words
    every door says, and the form puts that refusal beside the field
    (``web_refusals.refused``), so the form holds no second copy of the check.
    """

    field.validators = [rule for rule in field.validators if not isinstance(rule, _BOUNDS)]
    return field


_BOUNDS = (MinLengthValidator, MaxLengthValidator, MinValueValidator, MaxValueValidator)


# Fields a command carries for its own bookkeeping, never as what it changes.
_BOOKKEEPING = frozenset({"idempotency_key", "reason"})
# The words a switch's two values are pressed by.
SWITCH_VARIANTS = ("on", "off")


def own_fields(schema: dict[str, Any]) -> set[str]:
    """The fields a command's schema takes as what it changes."""

    return set(schema.get("properties", {})) - _BOOKKEEPING


def is_switch(schema: dict[str, Any]) -> bool:
    """Whether a command sets one thing to yes or no: its one own field is a boolean."""

    names = own_fields(schema)
    return len(names) == 1 and _one_type(whole_fields(schema)[next(iter(names))]).get("type") == "boolean"


def switch_field(spec: CapabilitySpec) -> str:
    """The field a switch sets, pressed as one of its declared ``on`` and
    ``off`` words; "" for any other command, or a switch without both words,
    which keeps its checkbox."""

    schema = command_schema(spec.command_type)
    if not is_switch(schema) or not set(SWITCH_VARIANTS) <= dict(spec.variants).keys():
        return ""
    return next(iter(own_fields(schema)))


class CapabilityCommandForm(forms.Form):
    """Presentation from JSON Schema; execution still validates canonically."""

    submission_errors: tuple[str, ...] = ()

    payload_names: tuple[str, ...] = ()
    payload_schema: ClassVar[dict[str, Any]] = {}
    effect = "read"
    # The fields offered as a choice of the records they name.
    record_names: tuple[str, ...] = ()
    # Each closed object drawn as its own fields: its name, to each field it
    # holds and whether that is asked for (``_INSIDE``).
    nested: ClassVar[Mapping[str, tuple[tuple[str, bool], ...]]] = {}
    # The field a switch sets, which its buttons post instead of a checkbox.
    switch = ""

    @property
    def primary_fields(self):
        drawn_elsewhere = {"__confirm_effect", "next", self.switch}
        return tuple(field for field in self.visible_fields() if field.name not in drawn_elsewhere)

    @property
    def confirmation_fields(self):
        return tuple(field for field in self.visible_fields() if field.name == "__confirm_effect")

    def __init__(self, *args, held_names: Mapping[str, str] | None = None, **kwargs):
        """``held_names`` is what the record calls each reference it holds, by
        field, so one whose thing has gone still shows by its name."""

        super().__init__(*args, **kwargs)
        for name in self.record_names:
            holding(self.fields[name], self.initial.get(name), (held_names or {}).get(name, ""))
        leave_off_unoffered(
            self, [name for name in self.record_names if referred(self.payload_schema[name]) is not None]
        )
        if not self.is_bound or not isinstance(self.data, MultiValueDict):
            return
        # And what ``{% write_token %}`` and a page's ``next`` draw beside the fields.
        allowed = set(self.fields) | {"csrfmiddlewaretoken", RETRY_KEY_FIELD, "next"}
        unknown = sorted(set(self.data) - allowed)
        repeated = sorted(
            name
            for name in self.data
            if name in allowed
            and name != "csrfmiddlewaretoken"
            and len(self.data.getlist(name)) > 1
            and not (name in self.fields and getattr(self.fields[name].widget, "allow_multiple_selected", False))
        )
        self.submission_errors = (
            *(ended(f"Unknown field: {name}") for name in unknown),
            *(ended(f"Repeated field: {name}") for name in repeated),
        )

    @override
    def clean(self):
        cleaned = super().clean()
        for message in self.submission_errors:
            self.add_error(None, message)
        if self.errors:
            return cleaned
        self.command_payload = {}
        for name in self.payload_names:
            value = self._gathered(name, cleaned) if name in self.nested else cleaned.get(name)
            # A blank the command may be sent without is left out, as a caller
            # leaves it out: it takes its default, or an update keeps it.
            if value in _BLANK and name not in self.payload_schema["required"]:
                continue
            self.command_payload[name] = value
        return cleaned

    def _gathered(self, name: str, cleaned: Mapping[str, Any]) -> dict[str, Any]:
        """A closed object as its fields were filled, leaving out a blank one
        it may be sent without."""

        value = {}
        for child, asked in self.nested[name]:
            path = f"{name}{_INSIDE}{child}"
            held = self._gathered(path, cleaned) if path in self.nested else cleaned.get(path)
            if held in _BLANK and not asked:
                continue
            value[child] = held
        return value


def holding(field: forms.ChoiceField, held: Any, named: str = "") -> None:
    """Offer what the record holds even when its source no longer lists it,
    so saving the form unchanged keeps it."""

    values = held if isinstance(held, (list, tuple)) else [held]
    kept = [str(value) for value in values if value not in (None, "")]
    field.choices = keeping_held(field.choices, [(value, named or value) for value in kept])


def grouped_choices(options: tuple[CommandTargetOption, ...]) -> tuple:
    """The choices under the type each is, where they are of more than one."""

    groups: dict[str, list[tuple[str, str]]] = {}
    for item in options:
        groups.setdefault(item.group, []).append((item.value, item.label))
    if len(groups) < 2:
        return tuple((item.value, item.label) for item in options)
    return tuple((group or "Other", tuple(choices)) for group, choices in sorted(groups.items()))


def _title(used: Mapping[str, Any]) -> dict[str, Any]:
    return {"title": used["title"]} if "title" in used else {}


def schema_fields(
    schema: dict[str, Any],
    properties: Mapping[str, dict[str, Any]] | None = None,
    *,
    required: frozenset[str] | None = None,
    offered: Mapping[str, tuple[CommandTargetOption, ...] | None] | None = None,
    said: Mapping[str, str] | None = None,
    nested: dict | None = None,
) -> dict[str, forms.Field]:
    """The fields an object's schema is drawn as, a closed object's as its fields.

    ``properties`` are the ones drawn (every one by default), ``offered`` the
    records each field that names them chooses from, ``said`` what each field
    means where that differs from its description, and ``nested`` gains what
    each closed object holds.
    """

    properties = whole_fields(schema) if properties is None else properties
    required = frozenset(schema.get("required", ())) if required is None else required
    offered = offered or {}
    said = said or {}
    nested = {} if nested is None else nested
    # A field is labeled from its name or the title its own use declares: the
    # title a shared alias's definition carries names the alias, for a viewer
    # offering its branches (``json_schemas``), never this field.
    used = schema.get("properties", {})
    fields: dict[str, forms.Field] = {}
    definitions = schema.get("$defs", {})
    for name, whole in properties.items():
        # A value that may be none, of a named type (a time of day), is that type or null.
        arms = [
            {
                key: value
                for key, value in definitions.get(arm.get("$ref", "").removeprefix(LOCAL), arm).items()
                if key not in {"title", "description"}
            }
            for arm in whole.get("anyOf", ())
        ]
        field = {**whole, "anyOf": arms} if arms else whole
        if name not in offered and _is_closed(field):
            fields |= _inside(name, field, definitions, required=name in required, nested=nested)
            continue
        fields[name] = _field_for_schema(
            name,
            {key: value for key, value in field.items() if key != "title"} | _title(used.get(name, {})),
            required=name in required,
            offered=offered.get(name),
            meaning=said.get(name),
        )
    return fields


def command_fields(command_type: type, *names: str) -> dict[str, forms.Field]:
    """The fields ``names`` of a command, drawn from its schema, for a form that
    posts beside another and so takes them as part of a larger whole."""

    schema = command_schema(command_type)
    whole = whole_fields(schema)
    return schema_fields(schema, {name: whole[name] for name in names}, said=meanings(command_type))


def command_form_class(
    spec: CapabilitySpec,
    *,
    target_options: tuple[CommandTargetOption, ...] | None = None,
    record_choices: Mapping[str, tuple[CommandTargetOption, ...] | None] | None = None,
    fields: tuple[str, ...] | None = None,
) -> type[CapabilityCommandForm]:
    """Build the complete browser form from one canonical capability spec.

    ``fields`` names the fields a page's own form draws, as
    ``modelform_factory`` takes them; a field left out is never sent.
    """

    schema = command_schema(spec.command_type)
    # What the command must be sent, which for an update is no field it keeps.
    required = frozenset(capability_schema(spec).get("required", ()))
    agents_only = for_agents(spec.command_type)
    if asked := sorted(agents_only & required):
        raise ValueError(f"{spec.name} must be sent {', '.join(asked)}, so a form cannot leave it out.")
    # A command that keeps its retry key is handed the one key the form
    # posts, which ``{% write_token %}`` draws (``web_writes``).
    properties = {
        name: field
        for name, field in whole_fields(schema).items()
        if name != IDEMPOTENCY_FIELD and name not in agents_only
    }
    if fields is not None:
        if unknown := sorted(set(fields) - set(properties)):
            raise ValueError(f"{spec.name} takes no {', '.join(unknown)} a person fills.")
        properties = {name: properties[name] for name in fields}
    offered = {
        name: (record_choices or {}).get(name)
        for name, field in properties.items()
        if named_records(field) is not None or referred(field) is not None or name in (record_choices or {})
    }
    for name, answer in among_now(spec.command_type).items():
        if (listed := offered.get(name)) is not None:
            allowed = frozenset(answer())
            offered[name] = tuple(option for option in listed if option.value in allowed)
    nested: dict[str, tuple[tuple[str, bool], ...]] = {}
    controls = schema_fields(
        schema, properties, required=required, offered=offered, said=meanings(spec.command_type), nested=nested
    )
    if fields is None:
        controls = _command_page_controls(spec, controls, target_options)
    if spec.effect.rank >= Effect.INFRASTRUCTURE_CHANGE.rank:
        controls["__confirm_effect"] = forms.BooleanField(
            label=(
                "I understand this changes a live system."
                if spec.effect is Effect.INFRASTRUCTURE_CHANGE
                else "I understand this stops or deletes something."
            )
        )
        # Read by templates/django/forms/field.html, which draws it as the callout closing the form.
        controls["__confirm_effect"].confirms = True
    return type(
        f"{schema.get('title', 'Capability')}WebForm",
        (CapabilityCommandForm,),
        {
            "payload_names": tuple(properties),
            "payload_schema": {**properties, "required": tuple(required)},
            "effect": spec.effect,
            "record_names": tuple(name for name, choices in offered.items() if choices is not None),
            "nested": nested,
            "switch": switch_field(spec),
            **controls,
        },
    )


def _command_page_controls(
    spec: CapabilitySpec,
    controls: dict[str, forms.Field],
    target_options: tuple[CommandTargetOption, ...] | None,
) -> dict[str, forms.Field]:
    """What the command's own page draws beside its fields: the record it acts
    on, when it acts on one, and where to go once it ran."""

    if spec.target_kind:
        options = {"label": spec.target_label or "Which one", "help_text": spec.target_help}
        if target_options is None:
            target_field = forms.IntegerField if spec.target_kind == "integer" else forms.CharField
            target: forms.Field = target_field(**options)
        else:
            empty_label = "Choose one…" if target_options else NOTHING_TO_CHOOSE
            target = forms.ChoiceField(choices=(("", empty_label), *grouped_choices(target_options)), **options)
        # Filled from the record a chosen target loads.
        controls = {"__target": target, **controls, "__expected_updated_at": ReadAt()}
    return {**controls, "next": forms.CharField(required=False, widget=forms.HiddenInput())}
