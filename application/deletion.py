"""Explicit, confirmed deletes for HQ domain records.

A record domain's delete is built from its declaration by
``application.records.deleter``; this module holds the one way a delete runs.
"""

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from calendars.models import Entry
from core.audit import operation_context

from .security import Capability, Principal


@dataclass(frozen=True)
class DeleteCommand:
    confirm: str


class ConflictError(ValueError):
    """The caller tried to delete a newer version of an object."""


def delete_record(
    model,
    *,
    lookup: dict,
    target: str,
    command: DeleteCommand,
    principal: Principal,
    capability: Capability | str,
    operation: str,
    type_name: str,
    expected_updated_at: str | None = None,
    after_commit=None,
) -> dict[str, Any]:
    principal.require(capability)
    if command.confirm != target:
        raise ValueError(f"confirm must exactly match target {target!r}")
    with transaction.atomic(), operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        try:
            obj = model.objects.select_for_update().get(**lookup)
        except model.DoesNotExist as exc:
            raise ValueError(f"{type_name} {target!r} was not found.") from exc
        if (
            expected_updated_at
            and obj.updated_at.isoformat() != expected_updated_at
        ):
            raise ConflictError(f"{type_name.title()} {target!r} changed after it was read.")
        label = str(obj)
        cleanup = after_commit(obj) if after_commit else None
        obj.delete()
        if cleanup:
            transaction.on_commit(cleanup)
    return {
        "ok": True,
        "deleted": {"type": type_name, "target": target, "label": label},
    }


def delete_calendar_entry(command, *, principal, current_key, expected_updated_at=None):
    target = str(current_key)
    return delete_record(
        Entry,
        lookup={"uid": target},
        target=target,
        command=command,
        principal=principal,
        capability=Capability.DELETE_CALENDAR,
        operation="calendar.entry.delete",
        type_name="calendar entry",
        expected_updated_at=expected_updated_at,
    )
