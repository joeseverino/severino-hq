"""What a timer HQ ships declares, read from its unit file under
deploy/systemd, so a threshold derived from a schedule and the schedule
itself cannot disagree."""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings

SYSTEMD = Path(settings.BASE_DIR) / "deploy" / "systemd"
_SPAN = re.compile(r"(\d+)\s*(h|min|s)")
_UNITS = {"h": timedelta(hours=1), "min": timedelta(minutes=1), "s": timedelta(seconds=1)}
# The calendar schedules this reads: systemd's shorthands, and a time each day.
_CALENDARS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
_EACH_DAY = re.compile(r"\*-\*-\* \d{2}:\d{2}(:\d{2})?")


def _declared(timer: str, key: str) -> str:
    for line in (SYSTEMD / timer).read_text().splitlines():
        name, _, value = line.partition("=")
        if name == key:
            return value.strip()
    raise ValueError(f"{timer} declares no {key}")


def span(timer: str, key: str) -> timedelta:
    """One time span ``timer`` declares, in systemd's notation."""

    value = _declared(timer, key)
    spans = _SPAN.findall(value)
    if not spans or _SPAN.sub("", value).strip():
        raise ValueError(f"{timer}: {key} is not a span this reads: {value!r}")
    return sum((int(count) * _UNITS[unit] for count, unit in spans), timedelta())


def every(timer: str) -> timedelta:
    """How often ``timer`` starts its unit, by its calendar schedule."""

    value = _declared(timer, "OnCalendar")
    if value in _CALENDARS:
        return _CALENDARS[value]
    if _EACH_DAY.fullmatch(value):
        return timedelta(days=1)
    raise ValueError(f"{timer}: OnCalendar is not a schedule this reads: {value!r}")
