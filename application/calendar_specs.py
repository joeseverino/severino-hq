"""The calendar's commands and its readable resource, declared beside it.

Registered on the calendar's host domain, so web, API and MCP read the same
declarations without a central list naming the calendar.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from calendars.models import Entry

from . import calendar_entries
from .calendar_entries import EntryCommand, save_entry
from .deletion import DeleteCommand, delete_calendar_entry
from .integration_specs import CapabilitySpec, ResourceSpec
from .resources import BoundedQuery
from .search_contracts import SearchDefinition
from .security import Capability


class AgendaQuery(BoundedQuery):
    start: date | None = None
    days: int = Field(default=14, ge=1, le=calendar_entries.AGENDA_DAYS)
    limit: int = Field(default=200, ge=1, le=500)


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "calendar.entry.create",
            "Put an entry on My Calendar.",
            "remote_write",
            Capability.WRITE_CALENDAR,
            EntryCommand,
            save_entry,
            subject_resource="calendar",
            label="Add to calendar",
        ),
        CapabilitySpec(
            "calendar.entry.update",
            "Change an entry on My Calendar.",
            "remote_write",
            Capability.WRITE_CALENDAR,
            EntryCommand,
            save_entry,
            "key",
            "calendar",
            target_label="Entry",
            target_help="The calendar entry to change.",
            label="Change calendar entry",
        ),
        CapabilitySpec(
            "calendar.entry.delete",
            "Remove an entry from My Calendar.",
            "destructive",
            Capability.DELETE_CALENDAR,
            DeleteCommand,
            delete_calendar_entry,
            "key",
            "calendar",
            target_label="Entry",
            target_help="The calendar entry to remove.",
            label="Remove calendar entry",
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "calendar",
            "Calendar",
            "What every calendar source holds over a window, and My Calendar's entries.",
            Capability.READ_CALENDAR,
            calendar_entries.list_agenda,
            AgendaQuery,
            calendar_entries.get_entry,
            "uid",
            not_found_errors=(calendar_entries.NotFoundError,),
            search=SearchDefinition(
                "calendar",
                Entry,
                "uid",
                ("title", "location", "notes"),
                label="Calendar",
                title_field="title",
            ),
            web_route="calendar:month",
        ),
    )
