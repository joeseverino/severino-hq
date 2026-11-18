"""What a plain record domain gets from its ``Records`` declaration.

Its commands, deletion, write and delete permissions, web write and health count
are read off ``application.domains``, so a record domain is declared there once
and never listed here. Every door, the web's forms included
(``writes.RecordFormMixin``), sends a command to the domain's ``save`` service
through its capability, which ends in the model's own ``full_clean``.
"""

from collections.abc import Callable, Mapping
from dataclasses import fields
from functools import cache
from typing import Any

from django import forms
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Field
from django.forms import ModelForm, modelform_factory
from django.utils.text import capfirst

from .command_fields import for_agents
from .contracts import Effect
from .deletion import DeleteCommand, RecordDeleted, delete_record
from .describe import meanings
from .domains import host_records, load, records_of
from .integration_specs import TARGET_KINDS, CapabilitySpec, command_schema
from .labels import adding, lower_first, one
from .record_declaration import Control, Records
from .security import Principal
from .ui import ended

# What a person calls the target, by kind.
_TARGET_NAMES = {"slug": "slug", "integer": "ID", "doc_id": "ID", "key": "key"}
# What an update says of a field its caller leaves out (``record_saves.written``).
KEPT = "A field left out keeps its value."
# What opens every record's form, and so what its update is called: "Edit an asset".
EDIT = "Edit"
# What opens its deletion's confirmation, and what confirms it.
DELETE = "Delete"


def deleter(resource: str) -> Callable[..., RecordDeleted]:
    """The confirmed delete for one record domain, in the host call contract."""

    def delete(
        command: DeleteCommand,
        *,
        principal: Principal,
        expected_updated_at: str | None = None,
        **target: Any,
    ) -> RecordDeleted:
        records = records_of(resource)
        keyword = TARGET_KINDS[records.target].keyword
        if set(target) != {keyword}:
            raise TypeError(f"{records.noun}.delete takes {keyword}.")
        value = target[keyword]
        return delete_record(
            load(records.model),
            lookup={records.lookup: value},
            target=str(value),
            command=command,
            type_name=records.noun,
            called=records.called,
            expected_updated_at=expected_updated_at,
            after_commit=load(records.on_delete) if records.on_delete else None,
        )

    delete.__name__ = f"delete_{resource}"
    return delete


def records_for(model: type) -> Records:
    """The record declaration whose model is ``model``."""

    for records in host_records():
        if load(records.model) is model:
            return records
    raise LookupError(f"No host domain declares records for {model.__name__}.")


# Each control a record's form draws, where Django's own says less than the
# browser offers: a date or time picker, a text box of a few lines, a list
# tall enough to choose several from.
_CONTROLS: dict[Control, Callable[[], forms.Widget]] = {
    Control.DATE: lambda: forms.DateInput(attrs={"type": "date"}),
    Control.TIME: lambda: forms.TimeInput(attrs={"type": "time"}),
    Control.LINES: lambda: forms.Textarea(attrs={"rows": 4}),
    Control.CHOOSE_SEVERAL: lambda: forms.SelectMultiple(attrs={"size": 6}),
}
# The control each kind of column takes unless its declaration names another.
_BY_KIND = {
    "DateField": Control.DATE,
    "TimeField": Control.TIME,
    "TextField": Control.LINES,
    "ManyToManyField": Control.CHOOSE_SEVERAL,
}


def _controlled(named: Mapping[str, Control]) -> Callable[..., forms.Field | None]:
    """The form field of a column, in the control ``named`` gives it or its
    kind takes; one limited to choices keeps Django's list."""

    def field(column: Field[Any, Any], **kwargs: Any) -> forms.Field | None:
        control = named.get(column.name) or _BY_KIND.get(column.get_internal_type())
        if control is not None and not column.choices:
            kwargs.setdefault("widget", _CONTROLS[control]())
        return column.formfield(**kwargs)

    return field


@cache
def record_form(records: Records, command: type | None = None) -> type[ModelForm[Any]]:
    """A plain record domain's form: the fields of its command (or of
    ``command``, a page whose save runs another), each drawn from the model's
    column of that name, on the declaration's ``form`` where it names one. A
    command field the model has no column for is refused as the form is built
    (Django's ``FieldError``), and so is a ``form`` that lists fields of its own."""

    command = command or load(records.command)
    base = load(records.form) if records.form else ModelForm
    if {"fields", "exclude"} & set(vars(getattr(base, "Meta", object))):
        raise ImproperlyConfigured(f"{base.__name__} lists fields; a record's form takes its command's.")
    taken = [item.name for item in fields(command) if item.name not in for_agents(command)]
    # A field's title is its label wherever it is drawn, the command's own page included.
    titled = {name: said["title"] for name, said in command_schema(command)["properties"].items() if "title" in said}
    return modelform_factory(
        load(records.model),
        form=base,
        fields=taken,
        labels=titled,
        formfield_callback=_controlled(dict(records.controls)),
    )


def described[F: ModelForm[Any]](form: F, command: type) -> F:
    """``form`` with the meaning ``command`` declares as each field's help, so
    a record's form says what that command's own page says (``meanings``).
    Help the form writes itself stays: it is for what only that form asks."""

    own = {*(form._meta.help_texts or {})}
    own |= {name for name, field in form.declared_fields.items() if field.help_text}
    for name, meaning in meanings(command).items():
        if name in form.fields and name not in own:
            form.fields[name].help_text = meaning
    return form


def as_an_update_declares(schema: dict[str, Any], always_sent: frozenset[str] = frozenset()) -> dict[str, Any]:
    """A command's schema as an update on a record that exists takes it: a
    field left out keeps what the record holds, so none has a default and only
    one the record cannot give back (``always_sent``) is required."""

    properties = {
        name: {key: value for key, value in declared.items() if key != "default"}
        for name, declared in schema.get("properties", {}).items()
    }
    updated = {**schema, "properties": properties}
    required = [name for name in schema.get("required", ()) if name in always_sent]
    if required:
        updated["required"] = required
    else:
        updated.pop("required", None)
    return updated


def _specs(records: Records) -> tuple[CapabilitySpec, ...]:
    title = records.title or records.noun
    command = load(records.command)
    # verb, declared, effect, permission, command, handler, summary, label, targeted
    verbs = (
        (
            "create",
            records.create,
            Effect.REMOTE_WRITE,
            records.write,
            command,
            records.save,
            ended(f"Add a new {title}"),
            adding(title),
            False,
        ),
        (
            "upsert",
            bool(records.upsert),
            Effect.REMOTE_WRITE,
            records.write,
            command,
            records.upsert,
            f"Add a new {title}, or update it if it already exists. On one that exists, {lower_first(KEPT)}",
            f"Add or update {one(title)}",
            False,
        ),
        (
            "update",
            True,
            Effect.REMOTE_WRITE,
            records.write,
            command,
            records.save,
            ended(f"Change one {title}"),
            f"{EDIT} {one(title)}",
            True,
        ),
        (
            "delete",
            True,
            Effect.DESTRUCTIVE,
            records.delete,
            DeleteCommand,
            deleter(records.resource),
            ended(f"Delete one {title}"),
            f"Delete {one(title)}",
            True,
        ),
    )
    wording = {verb: (summary, label) for verb, summary, label in records.wording}
    target_label = f"{capfirst(title)} {_TARGET_NAMES[records.target]}"
    return tuple(
        CapabilitySpec(
            f"{records.noun}.{verb}",
            _keeping(verb, wording.get(verb, ("", ""))[0] or summary),
            effect,
            permission,
            command_type,
            load(handler) if isinstance(handler, str) else handler,
            records.target if targeted else None,
            records.resource,
            target_label=target_label if targeted else "",
            target_help=ended(f"The {title} to {verb}") if targeted else "",
            label=wording.get(verb, ("", ""))[1] or label,
            verb={"update": EDIT, "delete": DELETE}.get(verb, ""),
            variants=tuple((variant, words) for of, variant, words in records.variants if of == verb),
        )
        for verb, declared, effect, permission, command_type, handler, summary, label, targeted in verbs
        if declared
    )


def _keeping(verb: str, summary: str) -> str:
    """An update's summary, which says what it does with a field left out,
    whatever its domain's own wording says first."""

    return f"{summary} {KEPT}" if verb == "update" else summary


def capability_specs() -> tuple[CapabilitySpec, ...]:
    return tuple(spec for records in host_records() for spec in _specs(records))


def counts() -> dict[str, int]:
    """How many records each domain holds, counting only what any reader may see."""

    return {
        records.resource: (load(records.visible)() if records.visible else load(records.model).objects.all()).count()
        for records in host_records()
    }
