"""The calendar's commands and its readable resource, declared beside it.

Registered on the calendar's host domain, so web, API and MCP read the same
declarations without a central list naming the calendar.
"""

from datetime import date
from typing import Annotated, TypedDict

from pydantic import Field

from hq.domains.calendars.models import Entry

from . import calendar_entries
from .answer_fields import UpdatedAt
from .calendar import EVENT_STATES
from .calendar_entries import (
    CHOOSE_SOURCE,
    CalendarEntryAbout,
    CalendarEntryInterval,
    CalendarEntryLocation,
    CalendarEntryRepeat,
    CalendarEntryTitle,
    CalendarEntryWeekdays,
    EntryCommand,
    SourceChoiceCommand,
    choose_source,
    save_entry,
)
from .deletion import DeleteCommand, delete_calendar_entry
from .describe import Describe, listed
from .integration_specs import CapabilitySpec, ResourceSpec
from .record_fields import Notes
from .records import EDIT, KEPT
from .resources import BoundedQuery, limit_words
from .search_contracts import SearchDefinition
from .security import Capability

# The agenda's page, bounded by how many entries a span may hold.
type AgendaLimit = Annotated[
    int,
    Field(ge=1, le=calendar_entries.AGENDA_PAGE, description=limit_words(calendar_entries.AGENDA_PAGE, paged=True)),
]


class AgendaQuery(BoundedQuery):
    start: date | None = Field(default=None, description="The first day the span covers; absent, today.")
    days: int = Field(
        default=14, ge=1, le=calendar_entries.AGENDA_DAYS, description="How many days the span covers, from start."
    )
    limit: AgendaLimit = 200


class AgendaItem(TypedDict):
    """One thing on the calendar in the span, from whichever source holds it."""

    source: Annotated[str, Describe("The id of the source it comes from, which showing or hiding a calendar takes.")]
    calendar: Annotated[str, Describe("The name of that source, as the calendar's list of them shows it.")]
    title: Annotated[str, Describe("What it is, in a line.")]
    starts: Annotated[
        str, Describe("When it starts: an ISO 8601 date for one that takes whole days, else a date-time.")
    ]
    ends: Annotated[
        str | None,
        Describe(
            "When it ends, in the terms `starts` takes and exclusive of that day or moment: whole days over the 3rd "
            "to the 5th end on the 6th. Null for a single day or moment."
        ),
    ]
    all_day: Annotated[bool, Describe("Whether it takes whole days, with no time of day.")]
    state: Annotated[
        str | None, Describe(f"For a plan, how it stands: {listed(sorted(EVENT_STATES))}; null for a plain fact.")
    ]
    detail: Annotated[str, Describe("More about it in a line, such as where it is and how often; empty for none.")]
    url: Annotated[str, Describe("Where HQ shows it, a path; empty for none.")]


class EntryRecord(TypedDict):
    """One of your own events, as its command writes it."""

    uid: Annotated[str, Describe("The event's id, which reading it, changing it and deleting it each take.")]
    title: CalendarEntryTitle
    # A day is read back as its ISO text, which the command's date type does not hold.
    starts_on: Annotated[str, Describe("The day it starts, an ISO 8601 date.")]
    ends_on: Annotated[
        str | None, Describe("The day it ends, an ISO 8601 date; null for one that ends the day it starts.")
    ]
    starts_at: Annotated[str | None, Describe("The time it starts, as `HH:MM`; null for one that takes the whole day.")]
    ends_at: Annotated[str | None, Describe("The time it ends, as `HH:MM`; null for none.")]
    location: CalendarEntryLocation
    notes: Notes
    repeat: CalendarEntryRepeat
    repeat_label: Annotated[str, Describe("How it repeats, in words, such as `Every 2 weeks on Mon`; empty for once.")]
    interval: CalendarEntryInterval
    weekdays: CalendarEntryWeekdays
    repeat_until: Annotated[str | None, Describe("The last day it repeats, an ISO 8601 date; null for no last day.")]
    about: CalendarEntryAbout
    url: Annotated[str, Describe("Its day on HQ's calendar, a path.")]
    updated_at: UpdatedAt


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
            label="Add an event",
        ),
        CapabilitySpec(
            "calendar.entry.update",
            f"Change one of your events on the calendar. {KEPT}",
            "remote_write",
            Capability.WRITE_CALENDAR,
            EntryCommand,
            save_entry,
            "key",
            "calendar",
            target_label="Event",
            target_help="The event to change.",
            label=f"{EDIT} an event",
            verb=EDIT,
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
            label="Delete an event",
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
            row_type=AgendaItem,
            record_type=EntryRecord,
        ),
    )
