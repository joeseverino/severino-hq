"""The calendar: every dated thing HQ holds, composed into one view.

Every domain, host section or extension alike, declares *sources* through
``PluginIntegration.calendars``. A source names a stream of dated things the
domain already holds (a certificate's expiry, a domain's registration, a
deploy) and answers one question: what falls between two days. The calendar
stores nothing a source can derive. What it owns is what only it knows: the
operator's own entries (``calendars.models.Entry``, the "My Calendar" source)
and which sources each operator has unchecked.

A source is read once per window and composed here, so a domain never learns
which others exist, and a source that fails is said beside its name in the
calendar list instead of taking the page down with it.

Three shapes cover everything a source emits:

- a **mark**: a dot on its day, read by colour. Many fit one day.
- an **item**: a line with a title (an appointment, an expiry).
- a **span**: an item over several days (a certificate's renewal window:
  opens, then expires).
  Spans keep a lane across the week so a bar lines up from cell to cell.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone

from .entity_links import web_url

logger = logging.getLogger("severino.calendar")

# "planned" is a commitment not yet due, "missed" one whose day has passed,
# "done" one kept. Blank is a plain fact with no plan behind it.
EVENT_STATES = frozenset({"", "done", "planned", "missed"})
# Dotted and lower-case, so a source id reads as whose it is ("example.sessions")
# and can sit in a URL or a preference row unescaped.
SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
# Chart series the palette has. A source asking for one keeps the colour its
# domain's charts already use; the rest are dealt from what is left.
SERIES_SLOTS = 10
# Rows a month cell shows, bars first, before the rest fold into "+N more".
CELL_ROWS = 3


@dataclass(frozen=True)
class CalendarEvent:
    """One dated thing a source holds.

    ``starts`` is a ``date`` for an all-day event and an aware ``datetime`` for
    a timed one. ``ends`` is exclusive, as in iCalendar: an all-day span over
    the 3rd to the 5th ends on the 6th. Blank means a single day or instant.
    """

    id: str
    title: str
    starts: date
    ends: date | None = None
    detail: str = ""
    url: str = ""
    state: str = ""
    # A dot on its day rather than a line: for what is many a day and read by
    # colour.
    mark: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.title.strip():
            raise ValueError("A calendar event needs an id and a title.")
        if self.state not in EVENT_STATES:
            raise ValueError(f"Unknown calendar event state {self.state!r}.")
        if isinstance(self.starts, datetime) and timezone.is_naive(self.starts):
            raise ValueError("A timed calendar event needs an aware start.")
        if self.ends is not None:
            if isinstance(self.ends, datetime) != isinstance(self.starts, datetime):
                raise ValueError("A calendar event starts and ends in the same terms.")
            if self.ends < self.starts:
                raise ValueError("A calendar event cannot end before it starts.")
        if self.mark and self.first_day != self.last_day:
            raise ValueError("A mark is one day; a longer event is a span.")
        # A source says where its record lives; an address that is not HQ's own
        # path or an http(s) URL is no link at all.
        object.__setattr__(self, "url", self.url if self.url.startswith("/") else web_url(self.url))

    @property
    def timed(self) -> bool:
        return isinstance(self.starts, datetime)

    @property
    def first_day(self) -> date:
        return _day(self.starts)

    @property
    def last_day(self) -> date:
        """The last day the event covers, inclusive."""

        if self.ends is None:
            return self.first_day
        if self.timed:
            last = timezone.localtime(self.ends)
            # An event ending at midnight ends the day before.
            return (last - timedelta(microseconds=1)).date() if last > self.starts else last.date()
        return max(self.first_day, self.ends - timedelta(days=1))

    @property
    def days(self) -> int:
        """How many days it covers, counting both ends."""

        return self.last_day.toordinal() - self.first_day.toordinal() + 1

    @property
    def span(self) -> bool:
        return self.days > 1

    @property
    def time_label(self) -> str:
        if not self.timed:
            return ""
        start = timezone.localtime(self.starts)
        return f"{start.hour % 12 or 12}{f':{start.minute:02d}' if start.minute else ''}{'a' if start.hour < 12 else 'p'}"

    def covers(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day


def _day(value: date) -> date:
    return timezone.localtime(value).date() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class CalendarSource:
    """One stream of dated things a domain holds.

    ``events(first, last)`` answers what covers any day from ``first`` to
    ``last`` inclusive. It is called once per view, so it reads what it needs
    in one pass over the window.
    """

    id: str
    label: str
    events: Callable[[date, date], Iterable[CalendarEvent]]
    description: str = ""
    # The source's own page: where its records are listed in full.
    url: str = ""
    # Checked until the operator unchecks it. History (what happened, rather
    # than what is coming) starts unchecked: the day panel shows it anyway.
    shown: bool = True
    # A chart series slot, to keep a colour the domain's charts already use;
    # 0 lets the calendar choose.
    slot: int = 0

    def __post_init__(self) -> None:
        if not SOURCE_ID.match(self.id):
            raise ValueError(f"Calendar source id {self.id!r} must be dotted and lower-case.")
        if not 0 <= self.slot <= SERIES_SLOTS:
            raise ValueError(f"Calendar source slot must be 0 to {SERIES_SLOTS}.")
        if not callable(self.events):
            raise ValueError("A calendar source's events must be callable.")


@dataclass(frozen=True)
class SourceView:
    """A source as the calendar list shows it."""

    id: str
    label: str
    group: str
    slot: int
    shown: bool
    description: str = ""
    url: str = ""
    count: int = 0
    # Why it could not be read this time, or blank.
    failure: str = ""


@dataclass(frozen=True)
class Placed:
    """An event with the source it came from."""

    event: CalendarEvent
    source: SourceView


@dataclass(frozen=True)
class SpanPiece:
    """One cell's part of a span: titled where it starts or a week begins."""

    placed: Placed
    starts_here: bool
    ends_here: bool
    titled: bool


@dataclass(frozen=True)
class CalendarCell:
    day: date
    in_month: bool
    today: bool
    past: bool
    marks: tuple[Placed, ...] = ()
    items: tuple[Placed, ...] = ()
    # One entry per lane of the week, so a bar keeps its row across cells;
    # None holds a lane open where this day has no span in it.
    lanes: tuple[SpanPiece | None, ...] = ()
    more: int = 0

    @property
    def weekend(self) -> bool:
        return self.day.weekday() >= 5

    @property
    def count(self) -> int:
        """Everything this day holds, drawn or folded."""

        return len(self.marks) + len(self.items) + self.more + sum(piece is not None for piece in self.lanes)

    @property
    def url(self) -> str:
        """This day, open beside its month."""

        from hq.platform.application.routes import reverse

        return f"{reverse('calendar:month')}?month={self.day:%Y-%m}&day={self.day.isoformat()}"


@dataclass(frozen=True)
class CalendarMonth:
    month: date
    weeks: tuple[tuple[CalendarCell, ...], ...]
    groups: tuple[tuple[str, tuple[SourceView, ...]], ...]
    today: date

    @property
    def previous(self) -> date:
        return (self.month - timedelta(days=1)).replace(day=1)

    @property
    def next(self) -> date:
        return (self.month + timedelta(days=32)).replace(day=1)

    @property
    def empty(self) -> bool:
        """Nothing checked falls in these weeks."""

        return not any(cell.count for week in self.weeks for cell in week)

    @property
    def is_current(self) -> bool:
        return self.month == self.today.replace(day=1)


@dataclass(frozen=True)
class CalendarDayView:
    day: date
    today: date
    groups: tuple[tuple[SourceView, tuple[CalendarEvent, ...]], ...] = field(default=())

    @property
    def previous(self) -> date:
        return self.day - timedelta(days=1)

    @property
    def next(self) -> date:
        return self.day + timedelta(days=1)


# ----- Sources ----------------------------------------------------------------


def calendar_sources() -> tuple[tuple[str, CalendarSource], ...]:
    """Every source, with the label of the domain that declares it.

    The calendar's own sources lead, then each domain's in nav order. A
    duplicate id is a composition error: two domains claiming one source would
    make an operator's unchecked choice ambiguous.
    """

    from .domains import all_domains
    from .calendar_entries import own_sources

    # The operator's own events lead under no heading: they are one line.
    found: list[tuple[str, CalendarSource]] = [("", source) for source in own_sources()]
    for domain in sorted(all_domains(), key=lambda domain: (domain.bar_order, domain.label)):
        provider = domain.integration.calendars
        if provider is None:
            continue
        found.extend((domain.label, source) for source in provider())
    seen: set[str] = set()
    for _group, source in found:
        if not isinstance(source, CalendarSource):
            raise TypeError(f"A calendar provider returned {type(source).__name__}, not CalendarSource.")
        if source.id in seen:
            raise ValueError(f"Two calendar sources claim the id {source.id!r}.")
        seen.add(source.id)
    return tuple(found)


def _slots(sources: Iterable[CalendarSource]) -> dict[str, int]:
    """A colour per source: its own when it asks, the least used otherwise.

    Sources checked by default are dealt first, so what shows together is told
    apart; unchecked history takes what is left. Dealt from the declarations
    alone, never from what an operator has checked, so checking a source never
    repaints the others.
    """

    sources = tuple(sources)
    # A colour a domain asks for is its identity (the colour its own charts
    # use): never dealt to another source while any other colour remains.
    asked = {source.slot for source in sources if source.slot}
    taken = [source.slot for source in sources if source.slot]
    dealt = {source.id: source.slot for source in sources if source.slot}
    for source in sorted((s for s in sources if not s.slot), key=lambda s: not s.shown):
        slot = min(
            range(1, SERIES_SLOTS + 1),
            key=lambda slot: (slot in asked, taken.count(slot), slot),
        )
        taken.append(slot)
        dealt[source.id] = slot
    return dealt


def _read(source: CalendarSource, first: date, last: date) -> tuple[tuple[CalendarEvent, ...], str]:
    """A source's events in the window, or why it could not say."""

    try:
        events = tuple(source.events(first, last))
    except Exception as exc:  # noqa: BLE001 - one source failing must not take the calendar down
        logger.warning(
            "calendar source failed: %s (%s)",
            source.id,
            type(exc).__name__,
            extra={"event": "calendar.source.failed", "source": source.id},
        )
        return (), "Could not be read just now"
    for event in events:
        if not isinstance(event, CalendarEvent):
            raise TypeError(f"Calendar source {source.id!r} returned {type(event).__name__}.")
    return tuple(event for event in events if event.last_day >= first and event.first_day <= last), ""


def gather(
    first: date, last: date, choices: Mapping[str, bool] | None = None
) -> tuple[tuple[tuple[str, tuple[SourceView, ...]], ...], tuple[Placed, ...]]:
    """The calendar list, grouped, and every event in the window.

    ``choices`` are the operator's own checks, by source id; a source they have
    not touched keeps its default. Events of unchecked sources are returned
    too: the grid leaves them out, and the day panel shows everything a day
    holds.
    """

    choices = choices or {}
    declared = calendar_sources()
    slots = _slots(source for _group, source in declared)
    groups: dict[str, list[SourceView]] = {}
    placed: list[Placed] = []
    for group, source in declared:
        events, failure = _read(source, first, last)
        view = SourceView(
            id=source.id,
            label=source.label,
            group=group,
            slot=slots[source.id],
            shown=choices.get(source.id, source.shown),
            description=source.description,
            url=source.url,
            count=len(events),
            failure=failure,
        )
        groups.setdefault(group, []).append(view)
        placed.extend(Placed(event, view) for event in events)
    return tuple((group, tuple(views)) for group, views in groups.items()), tuple(placed)


# ----- Month ------------------------------------------------------------------


def month_of(value: str | None, today: date) -> date:
    """``YYYY-MM`` as the first of its month; this month when absent or wrong."""

    try:
        year, month = (int(part) for part in str(value or "").split("-"))
        return date(year, month, 1)
    except ValueError:
        return today.replace(day=1)


def _grid(month: date) -> tuple[date, date]:
    """The six Sunday-first weeks a month is drawn in, first and last day."""

    first = month - timedelta(days=(month.weekday() + 1) % 7)
    return first, first + timedelta(days=41)


def calendar_month(
    month: date, *, choices: Mapping[str, bool] | None = None, today: date | None = None
) -> CalendarMonth:
    today = today or timezone.localdate()
    first, last = _grid(month)
    groups, placed = gather(first, last, choices)
    # Earlier first, then longer, then all-day before timed, so long bars
    # claim the top lanes.
    shown = sorted(
        (item for item in placed if item.source.shown),
        key=lambda item: (
            item.event.first_day,
            -item.event.days,
            item.event.timed,
            _sort_time(item.event),
            item.event.title,
        ),
    )
    # Everything not a span is one day's: bucketed once, so a cell is a lookup.
    on_day: dict[date, list[Placed]] = {}
    for item in shown:
        if not item.event.span:
            on_day.setdefault(item.event.first_day, []).append(item)
    spans = [item for item in shown if item.event.span]
    weeks = []
    for start in (first + timedelta(days=7 * week) for week in range(6)):
        days = [start + timedelta(days=offset) for offset in range(7)]
        lanes = _lanes(spans, days)
        weeks.append(tuple(_cell(day, month, today, on_day.get(day, []), lanes) for day in days))
    return CalendarMonth(month=month, weeks=tuple(weeks), groups=groups, today=today)


def _sort_time(event: CalendarEvent) -> Any:
    return timezone.localtime(event.starts).time() if event.timed else datetime.min.time()


def _lanes(spans: list[Placed], days: list[date]) -> list[list[Placed | None]]:
    """Spans in this week, each given the first lane free over all its days."""

    lanes: list[list[Placed | None]] = []
    for item in spans:
        covered = [index for index, day in enumerate(days) if item.event.covers(day)]
        if not covered:
            continue
        lane = next(
            (lane for lane in lanes if all(lane[index] is None for index in covered)),
            None,
        )
        if lane is None:
            lane = [None] * len(days)
            lanes.append(lane)
        for index in covered:
            lane[index] = item
    return lanes


def _cell(
    day: date,
    month: date,
    today: date,
    on_day: list[Placed],
    lanes: list[list[Placed | None]],
) -> CalendarCell:
    index = (day.weekday() + 1) % 7
    marks = tuple(item for item in on_day if item.event.mark)
    items = [item for item in on_day if not item.event.mark]
    drawn, hidden = lanes[:CELL_ROWS], lanes[CELL_ROWS:]
    pieces = tuple(
        None
        if lane[index] is None
        else SpanPiece(
            placed=lane[index],
            starts_here=lane[index].event.first_day == day,
            ends_here=lane[index].event.last_day == day,
            titled=lane[index].event.first_day == day or index == 0,
        )
        for lane in drawn
    )
    # A week's lanes take their rows in every cell of it, so bars stay level;
    # what is left holds lines, and the rest is counted, hidden bars included.
    room = max(CELL_ROWS - len(drawn), 0)
    return CalendarCell(
        day=day,
        in_month=day.month == month.month,
        today=day == today,
        past=day < today,
        marks=marks,
        items=tuple(items[:room]),
        lanes=pieces,
        more=max(len(items) - room, 0) + sum(lane[index] is not None for lane in hidden),
    )


# ----- Day --------------------------------------------------------------------


def calendar_day(
    day: date, *, choices: Mapping[str, bool] | None = None, today: date | None = None
) -> CalendarDayView:
    """Everything one day holds, by source, checked or not."""

    today = today or timezone.localdate()
    groups, placed = gather(day, day, choices)
    by_source: dict[str, list[CalendarEvent]] = {}
    for item in placed:
        if item.event.covers(day):
            by_source.setdefault(item.source.id, []).append(item.event)
    views = [view for _group, views in groups for view in views]
    return CalendarDayView(
        day=day,
        today=today,
        groups=tuple(
            (view, tuple(sorted(by_source[view.id], key=lambda event: (event.timed, _sort_time(event), event.title))))
            for view in views
            if by_source.get(view.id)
        ),
    )
