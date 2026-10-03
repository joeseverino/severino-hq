"""How HQ writes a moment, a day and an age.

One owner for the words, so a page, a chart tip and an extension all say a
time the same way. ``application.ui`` hands these on under the same names, and
``hq_sdk.ui`` hands them to extensions; ``core.templatetags.value_tags`` owns
the one filter that puts them in a page.
"""

from __future__ import annotations

from .timestamps import moment


def elapsed(stamp: str) -> str:
    """A provider's timestamp as an age, or as the fact that there is none."""

    from datetime import datetime, timezone as _tz

    from .ui import MISSING

    found = moment(stamp)
    if found is None:
        return MISSING
    if found > datetime.now(_tz.utc):
        return "just now"
    return ago(found)


def ago(moment) -> str:
    """How long ago something happened, in the one phrasing HQ uses: "5 days ago".

    One unit. An age is read at a glance, and "5 days, 15 hours ago" beside
    "5 days ago" is two phrasings of one fact; the exact moment is what
    ``when_exact`` says, and the ``when`` filter puts it behind the age.
    """

    from django.utils.timesince import timesince

    age = timesince(moment, depth=1)
    return "just now" if age.startswith("0\xa0minutes") or age.startswith("0 minutes") else f"{age} ago"


# How HQ writes a day and a time of day, as Django date-format strings. Spelled
# here and nowhere else: ``when``, ``when_day`` and ``when_exact`` are built from
# them, and ``config.formats`` hands the same ones to Django, so a datetime a
# template prints bare still reads this way.
DAY_FORMAT = "M j"
DAY_YEAR_FORMAT = "M j, Y"
CLOCK_FORMAT = "g:i A"
CLOCK_EXACT_FORMAT = "g:i:s A"
MOMENT_YEAR_FORMAT = f"{DAY_YEAR_FORMAT}, {CLOCK_FORMAT}"


def _local(value):
    """An aware datetime in the timezone HQ is read in; anything else as given."""

    from django.utils import timezone

    return timezone.localtime(value) if timezone.is_aware(value) else value


def _written(value, layout: str) -> str:
    from django.utils.dateformat import format as written

    return written(value, layout)


def when_day(value) -> str:
    """A date as HQ writes one: "Oct 3", and "Oct 3, 2025" outside this year.

    The year is said only when it is not the one being lived in, the same
    rule ``when`` keeps. A datetime is read as the day it falls on here.
    """

    from datetime import datetime

    from django.utils import timezone

    if isinstance(value, datetime):
        value = _local(value)
    this_year = value.year == timezone.localdate().year
    return _written(value, DAY_FORMAT if this_year else DAY_YEAR_FORMAT)


def when(value) -> str:
    """A moment as HQ writes one: "Oct 3, 9:29 AM", "Oct 3, 2025, 9:29 AM".

    Twelve-hour clock, in the timezone HQ is read in. A bare date has no time
    of day and reads as ``when_day`` writes it.
    """

    from datetime import datetime

    if not isinstance(value, datetime):
        return when_day(value)
    value = _local(value)
    return f"{when_day(value)}, {_written(value, CLOCK_FORMAT)}"


def when_exact(value) -> str:
    """A moment in full: "Oct 3, 2026, 9:29:15 AM CDT".

    Always the year, the seconds and the zone: what a ``title`` says behind an
    age, and what a detail page says of the one event it is about.
    """

    from datetime import datetime

    if not isinstance(value, datetime):
        return _written(value, DAY_YEAR_FORMAT)
    value = _local(value)
    exact = _written(value, f"{DAY_YEAR_FORMAT}, {CLOCK_EXACT_FORMAT}")
    zone = value.tzname() or ""
    return f"{exact} {zone}".strip()


def when_range(start, end) -> str:
    """A run of days as HQ writes one: "Oct 3 – Oct 9", "Oct 3 – Oct 9, 2025".

    Each end reads as ``when_day`` writes it. A year both ends share is said
    once, after the second; a run that crosses a year says each end's own. A
    run of one day is that day.
    """

    from datetime import datetime

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

    from datetime import datetime, timedelta, timezone as _tz

    from django.utils.timesince import timesince

    start = datetime(2000, 1, 1, tzinfo=_tz.utc)
    return timesince(start, start + max(delta, timedelta(0)))
