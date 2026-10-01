"""The host's calendar sources: what the estate is due, and what happened.

Each reads what its domain already derived (the estate's ``Expiry`` facts, the
history's ``Moment`` lines) and only says it in calendar terms.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta

from django.urls import reverse
from django.utils import timezone

from .calendar import CalendarEvent, CalendarSource
from .estate import Expiry, estate_reading
from .ui import counted

# How long before a registration's expiry renewing it by hand belongs on the
# calendar, when the registrar does not renew it on its own.
REGISTRATION_NOTICE_DAYS = 30


def _window(first: date, last: date) -> tuple[datetime, datetime]:
    zone = timezone.get_current_timezone()
    return (
        datetime.combine(first, time.min, tzinfo=zone),
        datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone),
    )


# ----- Estate -----------------------------------------------------------------


def estate_sources() -> tuple[CalendarSource, ...]:
    return (
        CalendarSource(
            id="estate.certificates",
            label="Certificates",
            events=certificate_events,
            description="Renewal windows of the certificates you renew, and any a provider let lapse.",
        ),
        CalendarSource(
            id="estate.registrations",
            label="Domain renewals",
            events=registration_events,
            description="When each domain's registration renews or runs out.",
            url=reverse("zones:index"),
        ),
    )


def _expiry_day(expiry: Expiry) -> date:
    return timezone.localtime(expiry.expires).date()


def certificate_events(first: date, last: date) -> Iterator[CalendarEvent]:
    """Only what needs a person: a certificate its provider renews is silent.

    A managed certificate is its renewal window, opening the day renewal is
    due to start and ending the day it expires. A provider-renewed one that
    did not renew is its expiry.
    """

    for expiry in estate_reading().operator_certificates:
        ends = _expiry_day(expiry)
        if expiry.managed:
            yield CalendarEvent(
                id=f"certificate:{expiry.resource_key}",
                title=f"Renew {expiry.subject}",
                starts=ends - timedelta(days=expiry.renewal_window_days),
                ends=ends + timedelta(days=1),
                detail=f"Expires {expiry.phrase}",
                url=expiry.url,
            )
        else:
            yield CalendarEvent(
                id=f"certificate:{expiry.source}:{expiry.subject}",
                title=f"{expiry.subject} expires",
                starts=ends,
                detail=f"{expiry.source} renews it and has not",
                url=expiry.url,
            )


def registration_events(first: date, last: date) -> Iterator[CalendarEvent]:
    """A registration that renews itself is a day; one that does not is a window."""

    for expiry in estate_reading().registrations:
        ends = _expiry_day(expiry)
        if expiry.auto_renew:
            yield CalendarEvent(
                id=f"registration:{expiry.subject}",
                title=f"{expiry.subject} renews",
                starts=ends,
                detail="Auto-renews at the registrar",
                url=expiry.url,
            )
            continue
        yield CalendarEvent(
            id=f"registration:{expiry.subject}",
            title=f"Renew {expiry.subject}",
            starts=ends - timedelta(days=REGISTRATION_NOTICE_DAYS),
            ends=ends + timedelta(days=1),
            detail=f"Registration expires {expiry.phrase}"
            + ("" if expiry.auto_renew is False else " · auto-renew not known"),
            url=expiry.url,
        )


# ----- History ----------------------------------------------------------------


def history_sources() -> tuple[CalendarSource, ...]:
    """What happened, day by day. Unchecked until wanted: the day panel shows it."""

    return (
        CalendarSource(
            id="history.deploys",
            label="Deploys",
            events=deploy_events,
            description="Every deploy GitHub records.",
            url=reverse("core:audit_list"),
            shown=False,
        ),
        CalendarSource(
            id="history.containers",
            label="Container starts",
            events=container_events,
            description="When each container was created, recreated or restarted.",
            url=reverse("control_plane:containers"),
            shown=False,
        ),
        CalendarSource(
            id="history.changes",
            label="Changes",
            events=change_events,
            description="What was changed through HQ, and what readings found changed outside it.",
            url=reverse("core:audit_list"),
            shown=False,
        ),
    )


def _moments(first: date, last: date, sources: set[str]):
    from .history import external

    since, until = _window(first, last)
    return external(sources, since=since, until=until)


def deploy_events(first: date, last: date) -> Iterator[CalendarEvent]:
    from .history import DEPLOYS

    for moment in _moments(first, last, {DEPLOYS}):
        yield CalendarEvent(
            id=f"deploy:{moment.url or moment.at.isoformat()}",
            title=moment.title,
            starts=moment.at,
            detail=moment.detail,
            url=moment.url,
            state="done",
        )


def container_events(first: date, last: date) -> Iterator[CalendarEvent]:
    from .history import CONTAINERS

    for moment in _moments(first, last, {CONTAINERS}):
        yield CalendarEvent(
            id=f"container:{moment.title}:{moment.at.isoformat()}",
            title=moment.title,
            starts=moment.at,
            detail=moment.detail,
            state="done",
        )


def change_events(first: date, last: date) -> Iterator[CalendarEvent]:
    """One line a day, by where the change was made: the day's history has the rest."""

    from .history import OUTSIDE_HQ, changes

    since, until = _window(first, last)
    rows = changes(since, until - timedelta(microseconds=1)).values_list("created_at", "source")
    per_day = Counter((timezone.localtime(at).date(), where) for at, where in rows)
    for (day, where), count in sorted(per_day.items()):
        outside = where == OUTSIDE_HQ
        yield CalendarEvent(
            id=f"changes:{where}:{day.isoformat()}",
            title=counted(count, "change outside HQ" if outside else "change through HQ",
                          "changes outside HQ" if outside else "changes through HQ"),
            starts=day,
            url=f"{reverse('core:audit_list')}?on={day.isoformat()}&source={where}",
            state="done",
        )
