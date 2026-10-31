"""Explicit, confirmed deletes for HQ domain records.

A record domain's delete is built from its declaration by
``application.records.deleter``; this module holds the one way a delete runs.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction

from hq.domains.calendars.models import Entry
from hq.platform.core.audit import operation_context

from .command_fields import Describe
from .record_saves import Held, held
from .security import Capability, Principal

# A delete runs for every record domain, so its refusals name none.
_RECORD = Held("That record was not found.", "That record changed after it was read.")


@dataclass(frozen=True, slots=True)
class DeleteCommand:
    confirm: Annotated[str, Describe("The record's identifier again, to confirm the delete.")]


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
    with (
        transaction.atomic(),
        operation_context(interface=principal.interface, actor=principal.actor, operation=operation),
    ):
        obj = held(model, _RECORD, lookup, expected_updated_at)
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
