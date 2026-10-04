"""What a plain record domain gets from its ``Records`` declaration.

The create, upsert, update and delete commands, deletion itself, the write and
delete permissions, the web write and the health count are all read off
``application.domains``, so a record domain is declared there once and never
listed here.

Two write paths, one rule. Agents, MCP, the HTTP API and the CLI send a
command to the domain's ``save`` service; the web binds a ``ModelForm`` and
calls ``save_form``. Both end in the model's own ``full_clean``, so a field
validator or constraint declared on the model holds on every path and no
adapter restates it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db import transaction
from django.forms import ModelForm

from hq.platform.core.audit import operation_context

from .deletion import DeleteCommand, delete_record
from .domains import Records, host_records, load, records_of
from .integration_specs import TARGET_KINDS, CapabilitySpec
from .security import Principal

# What a person calls the target, by kind.
_TARGET_NAMES = {"slug": "slug", "integer": "ID", "doc_id": "ID", "key": "key"}


def deleter(resource: str) -> Callable[..., dict[str, Any]]:
    """The confirmed delete for one record domain, in the host call contract."""

    def delete(
        command: DeleteCommand,
        *,
        principal: Principal,
        expected_updated_at: str | None = None,
        **target: Any,
    ) -> dict[str, Any]:
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
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation=f"{records.noun}.{verb}",
    ):
        instance.full_clean()
        return form.save()


def delete_instance(instance: Any, *, principal: Principal) -> dict[str, Any]:
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
        ("create", records.create, "remote_write", records.write, command, records.save,
         f"Create an HQ {title}.", f"Create {title}", False),
        ("upsert", bool(records.upsert), "remote_write", records.write, command, records.upsert,
         f"Idempotently create or update an HQ {title} by {records.lookup}.",
         f"Create or update {title}", False),
        ("update", True, "remote_write", records.write, command, records.save,
         f"Update an HQ {title}.", f"Update {title}", True),
        ("delete", True, "destructive", records.delete, DeleteCommand, deleter(records.resource),
         f"Delete a confirmed {title}.", f"Delete {title}", True),
    )
    wording = {verb: (summary, label) for verb, summary, label in records.wording}
    target_label = f"{title[0].upper()}{title[1:]} {_TARGET_NAMES[records.target]}"
    return tuple(
        CapabilitySpec(
            f"{records.noun}.{verb}",
            wording.get(verb, ("", ""))[0] or summary,
            effect,
            permission,
            command_type,
            load(handler) if isinstance(handler, str) else handler,
            records.target if targeted else None,
            records.resource,
            target_label=target_label if targeted else "",
            target_help=f"The {title} to {verb}." if targeted else "",
            label=wording.get(verb, ("", ""))[1] or label,
        )
        for verb, declared, effect, permission, command_type, handler, summary, label, targeted
        in verbs
        if declared
    )


def capability_specs() -> tuple[CapabilitySpec, ...]:
    return tuple(spec for records in host_records() for spec in _specs(records))


def counts() -> dict[str, int]:
    """How many records each domain holds, counting only what any reader may see."""

    return {
        records.resource: (
            load(records.visible)() if records.visible else load(records.model).objects.all()
        ).count()
        for records in host_records()
    }
