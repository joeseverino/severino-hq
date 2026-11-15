"""Explicit, confirmed deletes for HQ domain records.

A record domain's delete is built from its declaration by
``application.records.deleter``; this module holds the one way a delete runs.
"""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.db import transaction

from hq.domains.calendars.models import Entry

from .answer_fields import CallMessage
from .command_fields import Confirm, Describe, confirm
from .record_saves import Held, held
from .security import Principal

# A delete runs for every record domain, so its refusals name none.
_RECORD = Held("That record was not found.", "That record changed after it was read.")


@dataclass(frozen=True, slots=True)
class DeleteCommand:
    confirm: Confirm


class DeletedPart(TypedDict):
    """The record the call deleted."""

    type: Annotated[str, Describe("What type of record it was, such as `asset`.")]
    target: Annotated[str, Describe("The identifier the call named it by.")]
    label: Annotated[str, Describe("What it was called.")]


class RecordDeleted(TypedDict):
    """A record deleted for good."""

    ok: Literal[True]
    deleted: DeletedPart
    message: CallMessage


def delete_record(
    model,
    *,
    lookup: dict,
    target: str,
    command: DeleteCommand,
    type_name: str,
    called: str,
    expected_updated_at: str | None = None,
    after_commit=None,
) -> RecordDeleted:
    confirm(command.confirm, target)
    with transaction.atomic():
        obj = held(model, _RECORD, lookup, expected_updated_at)
        label = str(obj)
        cleanup = after_commit(obj) if after_commit else None
        obj.delete()
        if cleanup:
            transaction.on_commit(cleanup)
    return {
        "ok": True,
        "deleted": {"type": type_name, "target": target, "label": label},
        "message": f"{called} “{label}” deleted.",
    }


def delete_calendar_entry(
    command: DeleteCommand, *, principal: Principal, current_key: str, expected_updated_at: str | None = None
) -> RecordDeleted:
    target = str(current_key)
    return delete_record(
        Entry,
        lookup={"uid": target},
        target=target,
        command=command,
        type_name="calendar entry",
        called="Event",
        expected_updated_at=expected_updated_at,
    )
