"""How HQ reads a stamp someone else wrote, and writes a moment, a day and an age.

One owner, so every reader agrees on a stamp's edges and every page says a time
the same way. A bad stamp reads as ``None``; a caller for whom that is an error
raises its own.
"""

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Self, cast

lazy from django.utils import dateformat, timezone

from .figured import UNITS, Figure
lazy from .derivations import reached, since, this_year
lazy from .expiry import days_until

if TYPE_CHECKING:
    from .record_errors import Sentence

# How a stamp without an offset is read: "utc" is what every provider that
# omits one means; "keep" leaves it naive, for a caller comparing it only with
# stamps from the same source; "refuse" answers None, for one that must not guess.
Naive = Literal["utc", "keep", "refuse"]


def moment(
    stamp: object,
    *,
    naive: Naive = "utc",
    zero_is_never: bool = True,
) -> datetime | None:
    """``stamp`` as a datetime, or ``None`` when it does not name an instant."""

    # A datetime is taken as given, and the naive and zero-time rules still
    # apply to it.
    found = stamp if isinstance(stamp, datetime) else _parsed(stamp)
    # Go (and so Tailscale) writes ``0001-01-01T00:00:00Z`` for "never", which
    # read as an instant looks like a bug rather than a fact. No source HQ
    # reads uses year 1 for anything else.
    if found is None or (zero_is_never and found.date() == date.min):
        return None
    if found.tzinfo is not None or naive == "keep":
        return found
    if naive == "refuse":
        return None
    return found.replace(tzinfo=UTC)


def _parsed(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        # A trailing ``Z`` needs no rewriting: fromisoformat accepts it on
        # every Python HQ supports.
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def stamp_day(value: object) -> date | None:
    """``value`` as a calendar day: a date (a datetime's own day), or an ISO
    ``YYYY-MM-DD``; ``None`` when it names neither."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


# How HQ writes a day and a time of day, as Django date-format strings. Spelled
# here and nowhere else: ``when``, ``when_day`` and ``when_exact`` are built from
# them, and ``config.formats`` hands the same ones to Django, so a datetime a
# template prints bare still reads this way. The spaces inside a date and a
# time are no-break spaces (U+00A0), as Django's own ages are: "Dec" at the end
# of one line and "15" at the start of the next reads as two facts.
DAY_FORMAT = "M\xa0j"
DAY_YEAR_FORMAT = "M\xa0j,\xa0Y"
CLOCK_FORMAT = "g:i\xa0A"
CLOCK_EXACT_FORMAT = "g:i:s\xa0A"
MOMENT_YEAR_FORMAT = f"{DAY_YEAR_FORMAT}, {CLOCK_FORMAT}"
WEEKDAY_FORMAT = "l"
WEEKDAY_SHORT_FORMAT = "D"
MONTH_FORMAT = "F\xa0Y"
MONTH_SHORT_FORMAT = "M"
NUMERAL_FORMAT = "j"


def _local(value):
    """An aware datetime in the timezone HQ is read in; anything else as given."""

    return timezone.localtime(value) if timezone.is_aware(value) else value


def _written(value, layout: str) -> str:
    return dateformat.format(value, layout)


def when_day(value: date) -> Sentence:
    """A date as HQ writes one: "Oct 3", and "Oct 3, 2025" outside this year.

    The year is said only when it is not the one being lived in, the same
    rule ``when`` keeps. A datetime is read as the day it falls on here.
    """

    if isinstance(value, datetime):
        value = _local(value)
    # A date's words are HQ's, whatever the date.
    return cast("Sentence", _written(value, DAY_FORMAT if value.year == this_year() else DAY_YEAR_FORMAT))


# How near now a moment is said as how far it lies ("3 minutes ago", "in 3
# hours"); one further off is said as its day and time. The one rule for when
# a moment is relative: within a day an age is what a person reads, and past
# it the day says more. GitHub's ``relative-time`` keeps a threshold the same way.
NEAR = timedelta(days=1)


def clock(value: datetime) -> str:
    """The time of day a moment falls at here: "7:00 PM", "7:30 PM"."""

    return _written(_local(value), CLOCK_FORMAT)


def when_at(value: date) -> str:
    """A moment's day and time, "Oct 3, 9:29 AM", the year said only when it
    is not this one: what ``when`` says past a day, and what a sentence says
    after "for", "until" or "between", where an age would not read. A bare
    date is its day."""

    return f"{when_day(value)}, {clock(value)}" if isinstance(value, datetime) else when_day(value)


def when(value: date | str | None, now: datetime | None = None) -> str:
    """A moment as HQ writes one, on every page and in every sentence.

    Within ``NEAR`` of now, how far it lies: "just now", "5 minutes ago", "in
    3 hours". Further off, as ``when_at`` says it. A bare date is its day. A
    provider's stamp is read as it is written; one that is missing or does
    not parse is "never". ``now`` is the instant a derivation was made at.
    """

    found = moment(value) if isinstance(value, str) else value
    if found is None:
        return "never"
    return Figure(_when(found, now), epoch(found))


def _when(found: date, now: datetime | None) -> str:
    if not isinstance(found, datetime) or reached(found + NEAR, now=now) or not reached(found - NEAR, now=now):
        return when_at(found)
    # ``since`` holds the wording until the next whole unit, ahead or behind.
    age = since(found, now=now)
    if abs(age) < _MINUTE:
        return "just now" if age >= timedelta(0) else "in under a minute"
    return f"{Span(abs(age))} ago" if age >= timedelta(0) else f"in {Span(abs(age))}"


def epoch(found: date) -> float:
    """A moment as the number it is ordered by: its seconds since the epoch, a day's from its start in UTC."""

    at = found if isinstance(found, datetime) else datetime(found.year, found.month, found.day, tzinfo=UTC)
    return at.timestamp()


def when_exact(value: date) -> str:
    """A moment in full: "Oct 3, 2026, 9:29:15 AM CDT".

    Always the year, the seconds and the zone: what a ``title`` says behind
    every moment, and what a printed page, which has no title, says.
    """

    if not isinstance(value, datetime):
        return _written(value, DAY_YEAR_FORMAT)
    value = _local(value)
    exact = _written(value, f"{DAY_YEAR_FORMAT}, {CLOCK_EXACT_FORMAT}")
    zone = value.tzname() or ""
    return f"{exact}\xa0{zone}" if zone else exact


def weekday(value: date, *, short: bool = False) -> str:
    """The day of the week a moment fell on: "Monday", or "Mon" short."""

    return _written(_day_of(value), WEEKDAY_SHORT_FORMAT if short else WEEKDAY_FORMAT)


def weekday_initials(value: date) -> str:
    """A weekday as a narrow column heads it: "Mo". Two letters, since one
    gives two T's and two S's."""

    return weekday(value, short=True)[:2]


def month(value: date, *, short: bool = False) -> str:
    """The month a moment fell in, as a calendar heads it: "October 2026", or
    its name alone short ("Oct"), beside a day that already says the rest."""

    return _written(_day_of(value), MONTH_SHORT_FORMAT if short else MONTH_FORMAT)


def numeral(value: date) -> str:
    """The day of the month alone, as a calendar's cell says it: "3"."""

    return _written(_day_of(value), NUMERAL_FORMAT)


def _day_of(value: date) -> date:
    """A datetime as the day it falls on here; a date as it is."""

    return _local(value) if isinstance(value, datetime) else value


def length(moment: date, now: datetime | None = None) -> str:
    """How far ``moment`` lies from now, either way, as a ``Span``: "5 minutes",
    and "under a minute" inside the first, which an age never counts in
    seconds. A bare date is the day."""

    if not isinstance(moment, datetime):
        return when_day(moment)
    # ``since`` holds the wording until the next whole unit, ahead or behind.
    age = abs(since(moment, now=now))
    return "under a minute" if age < _MINUTE else Span(age)


def when_range(start: date, end: date) -> str:
    """A run of days as HQ writes one: "Oct 3 – Oct 9", "Oct 3 – Oct 9, 2025".

    Each end reads as ``when_day`` writes it. A year both ends share is said
    once, after the second; a run that crosses a year says each end's own. A
    run of one day is that day.
    """

    if isinstance(start, datetime):
        start = _local(start).date()
    if isinstance(end, datetime):
        end = _local(end).date()
    if start == end:
        return when_day(end)
    first = _written(start, DAY_FORMAT) if start.year == end.year else when_day(start)
    return f"{first} – {when_day(end)}"


_MINUTE = timedelta(minutes=1)
# Up to this many days a length of time is said in days: "46 days" is what a
# deadline is counted in. Past it a count of days means nothing at a glance.
DAYS_SAID_AS_DAYS = 90
# Each unit a length is said in under a day, with its length; past a day it is
# days, then calendar months and years.
_UNDER_A_DAY = (("hour", timedelta(hours=1)), ("minute", _MINUTE), ("second", timedelta(seconds=1)))
# Where calendar months are counted from, as Django's ``timesince`` counts them.
_COUNTED_FROM = datetime(2000, 1, 1, tzinfo=UTC)


class Span(Figure):
    """A length of time in words, the one way HQ says one: the largest unit
    it holds whole and that unit alone ("45 seconds", "3 hours", "46 days",
    "3 months", "2 years"), as GitHub's ``relative-time`` says a duration at
    one unit of precision, so two lengths side by side read on one scale. A
    no-break space keeps the count with its unit wherever it is printed. Its
    figure is the length in seconds."""

    def __new__(cls, length: timedelta) -> Self:
        length = max(length, timedelta(0))
        count, unit = _rung(length)
        return super().__new__(cls, f"{count:,}\xa0{unit if count == 1 else UNITS[unit]}", length.total_seconds())

    # Pickled as the length it says, so a cached read is said again the same way.
    def __getnewargs__(self) -> tuple[timedelta]:  # type: ignore[override]  # Figure declares two arguments
        return (timedelta(seconds=float(self.figure)),)


def _rung(length: timedelta) -> tuple[int, str]:
    if length.days > DAYS_SAID_AS_DAYS:
        end = _COUNTED_FROM + length
        months = (end.year - _COUNTED_FROM.year) * 12 + end.month - _COUNTED_FROM.month
        return (months // 12, "year") if months >= 12 else (months, "month")
    if length.days:
        return length.days, "day"
    return next(((length // size, unit) for unit, size in _UNDER_A_DAY if length >= size), (0, "second"))


def span(days: int) -> str:
    """A whole number of days as a length of time a person reads: "46 days", "2 years", "3 days overdue"."""

    said = Span(timedelta(days=abs(days)))
    # A count below nothing is that long past.
    return Figure(f"{said} overdue" if days < 0 else said, days)


def expiry_words(when: datetime | int, now: datetime | None = None) -> str:
    """When something runs out, from today: "expires in 3 days", "expires today", "expired 4 days ago".

    ``when`` is the moment it runs out, or the days left as ``days_until``
    counts them. A past expiry is a past tense, never a count below nothing.
    The controller's ``expiryWords`` says the same, and
    ``controller/providers/testdata/expiry-words.json`` holds both to one table.
    """

    days = when if isinstance(when, int) else days_until(when, now)
    if days > 0:
        return f"expires in {span(days)}"
    if days == 0:
        return "expires today"
    # days_until counts a moment past by under a day as -1, so the whole days
    # since are one fewer, truncated as ``ago`` truncates an age.
    since = -days - 1
    return f"expired {span(since)} ago" if since else "expired today"


def since_when(value: date) -> str:
    """Since when a state has held: "since 9:29 AM" today, else "since Oct 3".

    The form owns its "since", so a page never writes one before an age
    ("since 3 days ago")."""

    if isinstance(value, datetime) and _local(value).date() == _local(timezone.now()).date():
        return f"since {clock(value)}"
    return f"since {when_day(value)}"


# What the ``when`` and ``when_text`` filters can be asked for, and the words each form writes.
# A new form is added here and in docs/Design.md's table, nowhere else.
WHEN_FORMS = {
    "": when,  # "5 minutes ago" within a day of now, else "Oct 3, 9:29 AM"; a date reads "Oct 3"
    "at": when_at,  # "Oct 3, 9:29 AM" however near, after "from" or "until" in a sentence
    "day": when_day,  # the day a moment fell on
    "exact": when_exact,  # "Oct 3, 2026, 9:29:15 AM CDT", where no title shows: a printed page
    "weekday": weekday,  # "Monday"
    "dateline": lambda value: f"{weekday(value)}, {when_day(value)}",  # "Wednesday, Nov 18", a page's day
    "initials": weekday_initials,  # "Mo", where a column is narrow
    "month": month,  # "October 2026", the month a calendar shows
    "numeral": numeral,  # "3", the day of the month in a calendar's cell
    "length": length,  # "5 minutes", how far it lies from now either way
    "since": since_when,  # "since 9:29 AM" today, "since Oct 3" before
    "expiry": expiry_words,  # "expires in 3 days", "expired 4 days ago"
}
