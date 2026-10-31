"""The calendar's commands and its readable resource, declared beside it.

Registered on the calendar's host domain, so web, API and MCP read the same
declarations without a central list naming the calendar.
"""

from datetime import date

from pydantic import Field

from hq.domains.calendars.models import Entry

from . import calendar_entries
from .calendar_entries import CHOOSE_SOURCE, EntryCommand, SourceChoiceCommand, choose_source, save_entry
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
            "Put an event on the calendar.",
            "remote_write",
            Capability.WRITE_CALENDAR,
            EntryCommand,
            save_entry,
            subject_resource="calendar",
            label="Add event",
            wants="add an event",
        ),
        CapabilitySpec(
            "calendar.entry.update",
            "Change one of your events on the calendar.",
            "remote_write",
            Capability.WRITE_CALENDAR,
            EntryCommand,
            save_entry,
            "key",
            "calendar",
            target_label="Event",
            target_help="The event to change.",
            label="Edit event",
            verb="Edit",
            wants="edit the event {target}",
        ),
        CapabilitySpec(
            "calendar.entry.delete",
            "Delete one of your events from the calendar.",
            "destructive",
            Capability.DELETE_CALENDAR,
            DeleteCommand,
            delete_calendar_entry,
            "key",
            "calendar",
            target_label="Event",
            target_help="The event to delete.",
            label="Delete event",
            wants="delete the event {target}",
        ),
        CapabilitySpec(
            CHOOSE_SOURCE,
            "Show or hide one calendar source on the signed-in person's month.",
            "remote_write",
            Capability.SET_PREFERENCES,
            SourceChoiceCommand,
            choose_source,
            subject_resource="calendar",
            execution_notes=("Set whether the source shows rather than toggle it, so asking twice changes nothing.",),
            label="Show or hide a calendar",
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "calendar",
            "Calendar",
            "Everything on the calendar over a span of days, and your own events.",
            Capability.READ_CALENDAR,
            calendar_entries.list_agenda,
            AgendaQuery,
            calendar_entries.get_entry,
            "uid",
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
