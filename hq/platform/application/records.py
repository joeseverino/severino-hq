"""What a plain record domain gets from its ``Records`` declaration.

Its commands, deletion, write and delete permissions, web write and health count
are read off ``application.domains``, so a record domain is declared there once
and never listed here. Agents, MCP, the HTTP API and the CLI send a command to
the domain's ``save`` service and the web calls ``save_form``; both end in the
model's own ``full_clean``, so a rule on the model holds on every path.
"""

from collections.abc import Callable
from typing import Any

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import transaction
from django.forms import ModelForm

from hq.platform.core.audit import audit_operation

from .deletion import DeleteCommand, RecordDeleted, delete_record
from .describe import meanings
from .domains import Records, host_records, load, records_of
from .integration_specs import TARGET_KINDS, CapabilitySpec
from .json_schemas import choices
from .labels import adding, lower_first, one
from .security import Principal
from .ui import ended

# What a person calls the target, by kind.
_TARGET_NAMES = {"slug": "slug", "integer": "ID", "doc_id": "ID", "key": "key"}
# The commands every record domain gets.
_VERBS = frozenset({"create", "upsert", "update", "delete"})
# What an update says of a field its caller leaves out (``record_saves.written``).
KEPT = "A field left out keeps its value."
# What opens every record's form, and so what its update is called: "Edit an asset".
EDIT = "Edit"


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
            principal=principal,
            capability=records.delete,
            operation=f"{records.noun}.delete",
            type_name=records.noun,
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


def as_its_model_declares(command_type: type, schema: dict[str, Any]) -> dict[str, Any]:
    """A record command's schema with what its model says of each field.

    A field the model limits to choices lists them, each value with its label
    (``json_schemas.choices``), and one it holds as long text is
    ``format: textarea``. The model is the one declaration: its ``full_clean``
    refuses any other value on every path, so the schema only says so. Any
    other command's schema is returned as it is.
    """

    model = next((load(records.model) for records in host_records() if load(records.command) is command_type), None)
    if model is None:
        return schema
    names = {field.name: field for field in model._meta.get_fields() if field.concrete}
    properties = {
        name: _as_field_declares(names[name], declared) if name in names else declared
        for name, declared in schema.get("properties", {}).items()
    }
    return {**schema, "properties": properties}


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


def _bounds(field: Any) -> dict[str, Any]:
    """The tightest bounds a model field's validators hold a number to, as JSON Schema says them."""

    lows = [v.limit_value for v in field.validators if isinstance(v, MinValueValidator)]
    highs = [v.limit_value for v in field.validators if isinstance(v, MaxValueValidator)]
    return {**({"minimum": max(lows)} if lows else {}), **({"maximum": min(highs)} if highs else {})}


def _as_field_declares(field: Any, declared: dict[str, Any]) -> dict[str, Any]:
    declared = {**declared, **_bounds(field)}
    if field.choices:
        allowed = [(value, str(label)) for value, label in field.flatchoices]
        if field.blank and all(value != "" for value, _label in allowed):
            allowed.insert(0, ("", "Not set"))
        return {**declared, **choices(allowed)}
    if field.get_internal_type() == "TextField":
        return {**declared, "format": "textarea"}
    return declared


def says_only_its_title(spec: CapabilitySpec) -> bool:
    """Whether a capability's summary is the one a record command is given
    when its domain declares none: its title as a sentence ("Add a new
    asset."), which a page under that title does not repeat."""

    for records in host_records():
        verb = spec.name.removeprefix(f"{records.noun}.")
        if verb in _VERBS:
            return not {said: summary for said, summary, _label in records.wording}.get(verb)
    return False


def display_noun(records: Records) -> str:
    """What a message calls one record: "Content item", "Expense"."""

    name = records.title or records.noun
    return f"{name[0].upper()}{name[1:]}"


@transaction.atomic
def save_form(form: ModelForm[Any], *, principal: Principal) -> Any:
    """Create or update a plain record from a bound, valid ``ModelForm``.

    The form has already run the model's validation on the fields it shows;
    this runs ``full_clean`` on the whole instance, the same call the command
    path makes, so what the web stores was held to exactly the same rules.
    """

    instance = form.instance
    records = records_for(type(instance))
    principal.require(records.write)
    verb = "create" if instance._state.adding else "update"
    with audit_operation(operation=f"{records.noun}.{verb}", principal=principal):
        instance.full_clean()
        return form.save()


def delete_instance(instance: Any, *, principal: Principal) -> RecordDeleted:
    """Delete one record the web already holds, through its declared deleter.

    The person confirmed on the page, so the confirmation is the record's own
    target, which is what ``delete_record`` checks for.
    """

    records = records_for(type(instance))
    target = getattr(instance, records.lookup)
    return deleter(records.resource)(
        DeleteCommand(confirm=str(target)),
        principal=principal,
        **{TARGET_KINDS[records.target].keyword: target},
    )


def _specs(records: Records) -> tuple[CapabilitySpec, ...]:
    title = records.title or records.noun
    command = load(records.command)
    # verb, declared, effect, permission, command, handler, summary, label, targeted
    verbs = (
        (
            "create",
            records.create,
            "remote_write",
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
            "remote_write",
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
            "remote_write",
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
            "destructive",
            records.delete,
            DeleteCommand,
            deleter(records.resource),
            ended(f"Delete one {title}"),
            f"Delete {one(title)}",
            True,
        ),
    )
    wording = {verb: (summary, label) for verb, summary, label in records.wording}
    target_label = f"{title[0].upper()}{title[1:]} {_TARGET_NAMES[records.target]}"
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
            verb=EDIT if verb == "update" else "",
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
