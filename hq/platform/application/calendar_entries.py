"""My Calendar: the operator's own entries, and which sources they have checked.

Web, API and MCP all write through these commands, so an entry an agent adds
is the same audited write as one added on the page.
"""

import calendar as month_lengths
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import count
from typing import Annotated, Any, Literal, TypedDict

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from pydantic import Field, WithJsonSchema

from hq.domains.calendars.models import INTERVAL_MAXIMUM, INTERVAL_MINIMUM, Entry, Preference
from hq.platform.application.labels import dotted
from hq.platform.application.routes import reverse

from .calendar import CalendarEvent, CalendarSource, gather
from .command_fields import Checked, Choices, Describe, Reason, Refers, stored_in
from .derivations import derivation
from .integration_specs import Kept, keeps
from .moments import clock, in_units, weekday, when_day, when_range
from .projection import iso, page_of
from .record_errors import RecordNotFound
from .record_fields import Notes
from .record_saves import Held, as_sent, found, held, row, written
from .security import Principal, person
from .ui import ended
from .url_query import with_query
lazy from . import calendar
lazy from .calendar_specs import EntryRecord
lazy from .record_answers import EntrySaved

_ENTRY = Held("That calendar event was not found.", "This event changed after it was read.")

OWN_SOURCE = "calendar.mine"
# Occurrences a repeating entry may yield into one window, so a daily entry
# repeating for decades cannot turn a month into a long walk.
MAX_OCCURRENCES = 400


# ----- Reading ----------------------------------------------------------------


def own_sources() -> tuple[CalendarSource, ...]:
    return (
        CalendarSource(
            id=OWN_SOURCE,
            label="My events",
            events=entry_events,
            description="Events you or your agents added.",
            url=reverse("calendar:month"),
        ),
    )


# How many windows are kept current: the one the dashboard shows and the one
# last paged to.
WINDOWS = 2


@derivation("calendar.mine", reads=("calendars.Entry",), ahead=WINDOWS)
def entry_events(first: date, last: date) -> tuple[CalendarEvent, ...]:
    """Every occurrence of every entry that touches the window, derived once
    per change of the entries."""

    once = Q(repeat="") & Q(last_day__gte=first)
    repeating = ~Q(repeat="") & (Q(repeat_until__isnull=True) | Q(repeat_until__gte=first))
    entries = (
        Entry.objects.annotate(last_day=Coalesce("ends_on", "starts_on"))
        .filter(starts_on__lte=last)
        .filter(once | repeating)
    )
    return tuple(occurrence(entry, day) for entry in entries for day in occurrences(entry, first, last))


def _span_days(entry: Entry) -> int:
    return ((entry.ends_on or entry.starts_on) - entry.starts_on).days


def occurrences(entry: Entry, first: date, last: date) -> Iterator[date]:
    """The days an entry starts on, for every occurrence touching the window.

    An occurrence that starts before ``first`` but runs into it counts. A
    monthly entry on the 31st skips months without one, and a yearly one on
    February 29th skips years without one, as iCalendar does.
    """

    length = _span_days(entry)
    earliest = first - timedelta(days=length)
    stop = min(last, entry.repeat_until) if entry.repeat_until else last
    yielded = 0
    for day in _starts(entry):
        if day > stop or yielded >= MAX_OCCURRENCES:
            return
        if day >= earliest:
            yielded += 1
            yield day


def _starts(entry: Entry) -> Iterator[date]:
    """Every day the entry starts on, from its first, by how it repeats."""

    if not entry.repeat:
        return iter((entry.starts_on,))
    return _STEPS[entry.repeat](entry)


def _daily(entry: Entry) -> Iterator[date]:
    day = entry.starts_on
    while True:
        yield day
        day += timedelta(days=entry.interval)


def _weekly(entry: Entry) -> Iterator[date]:
    start = entry.starts_on
    days = entry.weekday_numbers or (start.weekday(),)
    week = start - timedelta(days=start.weekday())
    while True:
        yield from (day for day in (week + timedelta(days=weekday) for weekday in days) if day >= start)
        week += timedelta(weeks=entry.interval)


def _monthly(entry: Entry) -> Iterator[date]:
    start = entry.starts_on
    for index in count():
        months = start.month - 1 + index * entry.interval
        year, month = start.year + months // 12, months % 12 + 1
        if start.day <= month_lengths.monthrange(year, month)[1]:
            yield date(year, month, start.day)


def _yearly(entry: Entry) -> Iterator[date]:
    start = entry.starts_on
    for year in count(start.year, entry.interval):
        if start.day <= month_lengths.monthrange(year, start.month)[1]:
            yield date(year, start.month, start.day)


_STEPS = {
    Entry.Repeat.DAILY: _daily,
    Entry.Repeat.WEEKLY: _weekly,
    Entry.Repeat.MONTHLY: _monthly,
    Entry.Repeat.YEARLY: _yearly,
}


def occurrence(entry: Entry, day: date) -> CalendarEvent:
    last = day + timedelta(days=_span_days(entry))
    if entry.all_day:
        starts: date = day
        ends: date | None = last + timedelta(days=1) if last > day else None
    else:
        zone = timezone.get_current_timezone()
        starts = datetime.combine(day, entry.starts_at, tzinfo=zone)
        ends = datetime.combine(last, entry.ends_at, tzinfo=zone) if entry.ends_at else None
    return CalendarEvent(
        id=f"{entry.uid}:{day.isoformat()}",
        title=entry.title,
        starts=starts,
        ends=ends,
        detail=dotted(entry.location, repeat_label(entry)),
        url=with_query(reverse("calendar:month"), {"day": day.isoformat(), "entry": entry.uid}),
    )


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def repeat_label(entry: Entry) -> str:
    """How an entry repeats, as a person says it; blank when it does not."""

    if not entry.repeat:
        return ""
    unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[entry.repeat]
    every = f"Every {unit}" if entry.interval == 1 else f"Every {in_units(entry.interval, unit)}"
    if entry.repeat == Entry.Repeat.WEEKLY and entry.weekday_numbers:
        every += " on " + ", ".join(_WEEKDAY_NAMES[day] for day in entry.weekday_numbers)
    if entry.repeat_until:
        every += f" until {when_day(entry.repeat_until)}"
    return every


# ----- Writing ----------------------------------------------------------------


# An entry's fields as its command writes them and its record reads them back, each said once.
type CalendarEntryTitle = Annotated[str, Describe("What the entry is.")]
type CalendarEntryStartsOn = Annotated[date, Describe("The day it starts.")]
type CalendarEntryEndsOn = Annotated[
    date | None, Describe("The day it ends, for one that spans days.", api="Null for one that ends the day it starts.")
]
type CalendarEntryLocation = Annotated[str, Describe("Where it happens.", api="Empty when not recorded.")]
Repeat = Literal["", "daily", "weekly", "monthly", "yearly"]
type CalendarEntryRepeat = Annotated[
    Repeat, Choices(Entry.Repeat.choices), Describe("How often it repeats.", api="Empty for once.")
]
type CalendarEntryInterval = Annotated[
    int,
    Field(ge=INTERVAL_MINIMUM, le=INTERVAL_MAXIMUM),
    Describe("How far apart its times are, in what it repeats by: 2 with weekly is every other week."),
]
type CalendarEntryWeekdays = Annotated[
    Sequence[Annotated[int, Choices((int(day), month_lengths.day_abbr[day]) for day in month_lengths.Day)]],
    Describe("For one that repeats weekly, the days of the week it falls on.", api="Monday as 0. Empty otherwise."),
]
type CalendarEntryRepeatUntil = Annotated[
    date | None, Describe("The last day it repeats.", api="Null for no last day.")
]


def _of_the_clock(value: time) -> time:
    # A time column stores a time of day and no offset: SQLite refuses one.
    if value.tzinfo is not None:
        raise ValueError
    return value


type ClockTime = Annotated[
    time,
    Checked(_of_the_clock, Reason("time_of_day", "must be a time of day with no offset")),
    WithJsonSchema(
        {
            "type": "string",
            # RFC 3339's time without an offset, which its ``time`` format requires.
            "format": "partial-time",
            "pattern": r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]{1,6})?)?$",
        }
    ),
    Describe("A time of day, as the clock on the wall reads it."),
]
type CalendarEntryAbout = Annotated[
    str, Refers(Entry, "about"), Describe("What it is about.", api="A `type:key` reference; empty for nothing.")
]


@stored_in(Entry)
@dataclass(frozen=True, slots=True)
class EntryCommand:
    """One entry, as every surface states it."""

    title: CalendarEntryTitle
    starts_on: Annotated[CalendarEntryStartsOn, Field(title="Day")]
    ends_on: Annotated[CalendarEntryEndsOn, Field(title="Until day")] = None
    starts_at: Annotated[ClockTime | None, Field(title="From"), Describe("The time it starts; blank for all day.")] = (
        None
    )
    ends_at: Annotated[ClockTime | None, Field(title="To"), Describe("The time it ends.")] = None
    location: CalendarEntryLocation = ""
    notes: Notes = ""
    repeat: CalendarEntryRepeat = ""
    interval: Annotated[CalendarEntryInterval, Field(title="Every")] = 1
    weekdays: Annotated[CalendarEntryWeekdays, Field(title="On")] = ()
    repeat_until: Annotated[CalendarEntryRepeatUntil, Field(title="Stop repeating after")] = None
    about: CalendarEntryAbout = ""


def serialize_entry(entry: Entry) -> EntryRecord:
    return row(
        entry,
        EntryRecord,
        uid=str(entry.uid),
        starts_at=iso(entry.starts_at, timespec="minutes"),
        ends_at=iso(entry.ends_at, timespec="minutes"),
        repeat_label=repeat_label(entry),
        weekdays=list(entry.weekday_numbers),
        url=entry.get_absolute_url(),
    )


def _stored(uid: str) -> dict[str, Any]:
    """An entry as its command sends it: its weekdays as numbers."""

    entry = found(Entry, _ENTRY, {"uid": uid})
    return {**as_sent(entry, EntryCommand, {}), "weekdays": entry.weekday_numbers}


@keeps(Kept(_stored))
@transaction.atomic
def save_entry(
    command: EntryCommand,
    *,
    principal: Principal,
    current_key: str | None = None,
    expected_updated_at: str | None = None,
    sent: frozenset[str] | None = None,
) -> EntrySaved:
    if current_key is None:
        entry, created = Entry(), True
    else:
        entry, created = held(Entry, _ENTRY, {"uid": current_key}, expected_updated_at), False
    values = written(command, None if created else sent)
    if "title" in values:
        values["title"] = values["title"].strip()
    if "weekdays" in values:
        values["weekdays"] = ",".join(str(day) for day in sorted(set(values["weekdays"])))
    for name, value in values.items():
        setattr(entry, name, value)
    entry.full_clean()
    entry.save()
    said = ended(f"Event “{entry}” {'created' if created else 'updated'}")
    return {"ok": True, "created": created, "entry": serialize_entry(entry), "message": said}


def calendar_choices(user: Any) -> dict[str, bool]:
    found = Preference.objects.filter(user=user).values_list("choices", flat=True).first()
    return {str(key): bool(value) for key, value in (found or {}).items()}


CHOOSE_SOURCE = "calendar.source.choose"


@dataclass(frozen=True, slots=True)
class SourceChoiceCommand:
    """One calendar source, and whether the month shows it."""

    source: Annotated[str, Describe("The calendar source.")]
    shown: Annotated[bool, Describe("Whether the month shows it.")]


class SourcesChosen(TypedDict):
    """Which calendar sources you show, as the call left them."""

    ok: Literal[True]
    choices: Annotated[dict[str, bool], Describe("Whether each calendar source is shown, by its name.")]


@transaction.atomic
def choose_source(command: SourceChoiceCommand, *, principal: Principal) -> SourcesChosen:
    """Check or uncheck one source for the signed-in person."""

    user = person(principal)
    if command.source not in {source.id for _group, source in calendar.calendar_sources()}:
        raise RecordNotFound("No calendar source has that name.")
    preference, _ = Preference.objects.select_for_update().get_or_create(user=user)
    preference.choices = {**preference.choices, command.source: bool(command.shown)}
    preference.save(update_fields=("choices", "updated_at"))
    return {"ok": True, "choices": calendar_choices(user)}


# ----- Agenda -----------------------------------------------------------------

# The longest window one read covers: two months is a planning horizon, and a
# caller wanting more asks twice rather than walking every source over a year.
AGENDA_DAYS = 62
# A span's events are read whole and placed in order, so a page may hold more than an API page.
AGENDA_PAGE = 500


def list_agenda(*, start: date | None = None, days: int = 14, limit: int = 200) -> dict[str, Any]:
    """Everything every source holds from ``start`` for ``days`` days, in order.

    The agent's view of the calendar: checked or not, since an agent asking
    "what is coming" is asking about all of it.
    """

    first = start or timezone.localdate()
    last = first + timedelta(days=min(max(days, 1), AGENDA_DAYS) - 1)
    _groups, placed = gather(first, last)
    items = sorted(
        placed,
        key=lambda item: (item.event.first_day, item.event.timed, str(item.event.starts), item.event.title),
    )
    paged = page_of(items, limit, maximum=AGENDA_PAGE)
    shown = [
        {
            "source": item.source.id,
            "calendar": item.source.label,
            "title": item.event.title,
            "starts": iso(item.event.starts),
            "ends": iso(item.event.ends),
            "all_day": not item.event.timed,
            "state": item.event.state or None,
            "detail": item.event.detail,
            "url": item.event.url,
        }
        for item in paged["items"]
    ]
    return {**paged, "start": iso(first), "end": iso(last), "items": shown}


def get_entry(uid: str) -> dict[str, Any]:
    return serialize_entry(found(Entry, _ENTRY, {"uid": uid}))


def upcoming(entry: Entry, *, count: int = 5, today: date | None = None) -> tuple[date, ...]:
    """The next days the entry falls on, from today."""

    first = today or timezone.localdate()
    found: list[date] = []
    for day in _starts(entry):
        if (entry.repeat_until and day > entry.repeat_until) or len(found) >= count:
            break
        if day + timedelta(days=_span_days(entry)) >= first:
            found.append(day)
        if not entry.repeat:
            break
    return tuple(found)


def when_label(entry: Entry) -> str:
    """When an entry is, as a person says it: "Wed, Sep 30 · 3:00 PM – 4:00 PM"."""

    if entry.ends_on and entry.ends_on != entry.starts_on:
        days = when_range(entry.starts_on, entry.ends_on)
    else:
        days = f"{weekday(entry.starts_on, short=True)}, {when_day(entry.starts_on)}"
    times = [clock(datetime.combine(entry.starts_on, at)) for at in (entry.starts_at, entry.ends_at) if at]
    return dotted(days, " – ".join(times if entry.starts_at else ()))
