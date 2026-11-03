"""My Calendar: the operator's own entries, and which sources they have checked.

Web, API and MCP all write through these commands, so an entry an agent adds
is the same audited write as one added on the page.
"""

import calendar as month_lengths
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import count
from typing import Annotated, Any, Literal, TypedDict, cast

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from pydantic import Field

from hq.domains.calendars.models import INTERVAL_MAXIMUM, INTERVAL_MINIMUM, Entry, Preference
from hq.platform.application.labels import dotted
from hq.platform.application.routes import reverse
from hq.platform.core.audit import audit_operation

from .calendar import CalendarEvent, CalendarSource, gather
from .command_fields import Describe, Refers
from .derivations import derivation
from .describe import listed
from .integration_specs import Kept, keeps
from .moments import when_day
from .projection import window
from .record_errors import RecordNotFound
from .record_fields import Notes
from .record_saves import Held, as_sent, found, held, written
from .security import Capability, Principal, person
from .ui import counted
lazy from . import calendar
lazy from .calendar_specs import EntryRecord
lazy from .record_answers import EntrySaved
lazy from .references import dangling

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
        url=f"{reverse('calendar:month')}?day={day.isoformat()}&entry={entry.uid}",
    )


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def repeat_label(entry: Entry) -> str:
    """How an entry repeats, as a person says it; blank when it does not."""

    if not entry.repeat:
        return ""
    unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[entry.repeat]
    every = f"Every {unit}" if entry.interval == 1 else f"Every {counted(entry.interval, unit)}"
    if entry.repeat == Entry.Repeat.WEEKLY and entry.weekday_numbers:
        every += " on " + ", ".join(_WEEKDAY_NAMES[day] for day in entry.weekday_numbers)
    if entry.repeat_until:
        every += f" until {when_day(entry.repeat_until)}"
    return every


# ----- Writing ----------------------------------------------------------------


# An entry's fields as its command writes them and its record reads them back, each said once.
type CalendarEntryTitle = Annotated[str, Describe("What the entry is.")]
type CalendarEntryStartsOn = Annotated[date, Describe("The day it starts, an ISO 8601 date.")]
type CalendarEntryEndsOn = Annotated[
    date | None, Describe("The day it ends, an ISO 8601 date; null for one that ends the day it starts.")
]
type CalendarEntryLocation = Annotated[str, Describe("Where it happens; empty when not recorded.")]
Repeat = Literal["", "daily", "weekly", "monthly", "yearly"]
type CalendarEntryRepeat = Annotated[
    Repeat,
    Describe(f"How often it repeats: {listed(Entry.Repeat.values)}; empty for once."),
]
type CalendarEntryInterval = Annotated[
    int,
    Field(ge=INTERVAL_MINIMUM, le=INTERVAL_MAXIMUM),
    Describe("How far apart its times are, in what `repeat` names: 2 with `weekly` is every other week."),
]
type CalendarEntryWeekdays = Annotated[
    Sequence[int],
    Describe("For one that repeats weekly, the days of the week it falls on, Monday as 0; else empty."),
]
type CalendarEntryRepeatUntil = Annotated[
    date | None, Describe("The last day it repeats, an ISO 8601 date; null for no last day.")
]
type CalendarEntryAbout = Annotated[
    str, Refers(Entry, "about"), Describe("What it is about, as `type:key`; empty for nothing.")
]


@dataclass(frozen=True, slots=True)
class EntryCommand:
    """One entry, as every surface states it."""

    title: CalendarEntryTitle
    starts_on: CalendarEntryStartsOn
    ends_on: CalendarEntryEndsOn = None
    starts_at: Annotated[time | None, Describe("The time it starts; blank for all day.")] = None
    ends_at: Annotated[time | None, Describe("The time it ends.")] = None
    location: CalendarEntryLocation = ""
    notes: Notes = ""
    repeat: CalendarEntryRepeat = ""
    interval: CalendarEntryInterval = 1
    weekdays: CalendarEntryWeekdays = ()
    repeat_until: CalendarEntryRepeatUntil = None
    about: CalendarEntryAbout = ""


def serialize_entry(entry: Entry) -> EntryRecord:
    return {
        "uid": str(entry.uid),
        "title": entry.title,
        "starts_on": entry.starts_on.isoformat(),
        "ends_on": entry.ends_on.isoformat() if entry.ends_on else None,
        "starts_at": entry.starts_at.isoformat(timespec="minutes") if entry.starts_at else None,
        "ends_at": entry.ends_at.isoformat(timespec="minutes") if entry.ends_at else None,
        "location": entry.location,
        "notes": entry.notes,
        # Every write passes EntryCommand, whose type holds repeat to these.
        "repeat": cast(Repeat, entry.repeat),
        "repeat_label": repeat_label(entry),
        "interval": entry.interval,
        "weekdays": list(entry.weekday_numbers),
        "repeat_until": entry.repeat_until.isoformat() if entry.repeat_until else None,
        "about": entry.about,
        "url": entry.get_absolute_url(),
        "updated_at": entry.updated_at.isoformat(),
    }


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
    principal.require(Capability.WRITE_CALENDAR)
    operation = "calendar.entry.create" if current_key is None else "calendar.entry.update"
    with audit_operation(operation=operation, principal=principal):
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
    return {"ok": True, "created": created, "entry": serialize_entry(entry)}


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
    """Which calendar sources the person shows, as the call left them."""

    ok: Literal[True]
    choices: Annotated[dict[str, bool], Describe("Whether each calendar source is shown, by its name.")]


@transaction.atomic
def choose_source(command: SourceChoiceCommand, *, principal: Principal) -> SourcesChosen:
    """Check or uncheck one source for the signed-in person.

    Not audited: a view preference is the person arranging their own page,
    not a change to anything HQ holds.
    """

    principal.require(Capability.SET_PREFERENCES)
    user = person(principal)
    if command.source not in {source.id for _group, source in calendar.calendar_sources()}:
        raise RecordNotFound("No calendar source has that name.")
    preference, _ = Preference.objects.select_for_update().get_or_create(user=user)
    preference.choices = {**preference.choices, command.source: bool(command.shown)}
    preference.save(update_fields=("choices", "updated_at"))
    return {"ok": True, "choices": calendar_choices(user)}


def attention() -> tuple[Any, ...]:
    """An event about something HQ no longer has, for the queue."""

    return dangling(Entry)


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
    page, paged = window(items, limit, maximum=AGENDA_PAGE)
    shown = [
        {
            "source": item.source.id,
            "calendar": item.source.label,
            "title": item.event.title,
            "starts": item.event.starts.isoformat(),
            "ends": item.event.ends.isoformat() if item.event.ends else None,
            "all_day": not item.event.timed,
            "state": item.event.state or None,
            "detail": item.event.detail,
            "url": item.event.url,
        }
        for item in page
    ]
    return {**paged, "start": first.isoformat(), "end": last.isoformat(), "items": shown}


def get_entry(uid: str) -> dict[str, Any]:
    return serialize_entry(found(Entry, _ENTRY, {"uid": uid}))


def entry_command(values: dict[str, Any]) -> EntryCommand:
    """A form's cleaned values as the command every surface writes with."""

    fields = EntryCommand.__dataclass_fields__
    return EntryCommand(**{name: value for name, value in values.items() if name in fields})


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


def _meridiem(value: Any) -> str:
    return "AM" if value.hour < 12 else "PM"


def _clock(value: time) -> str:
    return f"{value.hour % 12 or 12}{f':{value.minute:02d}' if value.minute else ''}"


def when_label(entry: Entry) -> str:
    """When an entry is, as a person says it: "Wed, Sep 30 · 3–4 PM"."""

    first = f"{entry.starts_on:%a}, {entry.starts_on:%b} {entry.starts_on.day}"
    if entry.ends_on and entry.ends_on != entry.starts_on:
        days = f"{entry.starts_on:%b} {entry.starts_on.day} – {entry.ends_on:%b} {entry.ends_on.day}"
    else:
        days = first
    if entry.starts_at is None:
        return days
    if entry.ends_at is None:
        return dotted(days, f"{_clock(entry.starts_at)} {_meridiem(entry.starts_at)}")
    start = _clock(entry.starts_at)
    if _meridiem(entry.starts_at) != _meridiem(entry.ends_at):
        start += f" {_meridiem(entry.starts_at)}"
    return dotted(days, f"{start}–{_clock(entry.ends_at)} {_meridiem(entry.ends_at)}")
