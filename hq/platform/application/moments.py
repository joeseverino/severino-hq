"""How HQ writes a moment, a day and an age.

One owner for the words, so a page, a chart tip and an extension all say a
time the same way. ``hq_sdk.ui`` hands these to extensions, and
``core.templatetags.value_tags`` owns the one filter that puts them in a page.
"""

from datetime import UTC, date, datetime, timedelta

lazy from django.utils import dateformat, timezone
lazy from django.utils.timesince import timesince

from .timestamps import moment as _stamp
lazy from .derivations import reached, since, this_year
lazy from .ui import MISSING, counted


def elapsed(stamp: str) -> str:
    """A provider's timestamp as an age, or as the fact that there is none."""

    found = _stamp(stamp)
    if found is None:
        return MISSING
    if not reached(found):
        return "just now"
    return ago(found)


def ago(moment: date | str, now: datetime | None = None) -> str:
    """How long ago something happened, in the one phrasing HQ uses: "5 days ago".

    One unit. An age is read at a glance, and "5 days, 15 hours ago" beside
    "5 days ago" is two phrasings of one fact; the exact moment is what
    ``when_exact`` says, and the ``when`` filter puts it behind the age.
    A provider's stamp is read as it is written; one that is empty or does not
    parse is "never". ``now`` is the instant a derivation was made at.
    """

    found = _stamp(moment) if isinstance(moment, str) else moment
    if found is None:
        return "never"
    if isinstance(found, datetime):
        # The wording changes at the next whole unit of age; ``since`` says so to
        # the derivation that read it, which is why a datetime goes through it.
        since(found, now=now)
        age = timesince(found, now, depth=1)
    else:
        age = timesince(found, depth=1)
    return "just now" if age.startswith(("0\xa0minutes", "0 minutes")) else f"{age} ago"


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


def when_day(value: date) -> str:
    """A date as HQ writes one: "Oct 3", and "Oct 3, 2025" outside this year.

    The year is said only when it is not the one being lived in, the same
    rule ``when`` keeps. A datetime is read as the day it falls on here.
    """

    if isinstance(value, datetime):
        value = _local(value)
    return _written(value, DAY_FORMAT if value.year == this_year() else DAY_YEAR_FORMAT)


def when(value: date) -> str:
    """A moment as HQ writes one: "Oct 3, 9:29 AM", "Oct 3, 2025, 9:29 AM".

    Twelve-hour clock, in the timezone HQ is read in. A bare date has no time
    of day and reads as ``when_day`` writes it.
    """

    if not isinstance(value, datetime):
        return when_day(value)
    value = _local(value)
    return f"{when_day(value)}, {_written(value, CLOCK_FORMAT)}"


def when_exact(value: date) -> str:
    """A moment in full: "Oct 3, 2026, 9:29:15 AM CDT".

    Always the year, the seconds and the zone: what a ``title`` says behind an
    age, and what a detail page says of the one event it is about.
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
    """How far ``moment`` lies from now, either way, in ``ago``'s one unit:
    "5 minutes", and "under a minute" inside the first. A bare date is the day."""

    if not isinstance(moment, datetime):
        return when_day(moment)
    # ``since`` holds the wording until the next whole unit, ahead or behind.
    start = datetime(2000, 1, 1, tzinfo=UTC)
    said = timesince(start, start + abs(since(moment, now=now)), depth=1)
    return "under a minute" if said.startswith(("0\xa0minutes", "0 minutes")) else said


def ahead(moment: date, now: datetime | None = None) -> str:
    """A moment to come as how soon it is: "in 3 days"; one already past as
    ``ago`` says it. A bare date is the day."""

    if not isinstance(moment, datetime):
        return when_day(moment)
    if reached(moment, now=now):
        return ago(moment, now)
    return f"in {length(moment, now)}"


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


def duration(delta) -> str:
    """A length of time in the phrasing ``ago`` uses, without the "ago"."""

    start = datetime(2000, 1, 1, tzinfo=UTC)
    return timesince(start, start + max(delta, timedelta(0)))


# Up to this many days a length of time is said in days: "46 days" is what a
# deadline is counted in. Past it the count stops meaning anything at a glance
# and the length is said in months and years.
DAYS_SAID_AS_DAYS = 90


def span(days: int) -> str:
    """A whole number of days as a length of time a person reads: "46 days", "2 years, 11 months", "3 days overdue"."""

    # Past a few weeks, a no-break space keeps the number with its unit ("2\xa0weeks").
    said = counted(abs(days), "day") if abs(days) <= DAYS_SAID_AS_DAYS else duration(timedelta(days=abs(days)))
    # A count below nothing is that long past.
    return f"{said} overdue" if days < 0 else said
