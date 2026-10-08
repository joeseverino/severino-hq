"""The host's calendar sources: what the estate is due, and what happened.

Each reads what its domain already derived (the estate's ``Expiry`` facts, the
history's ``Moment`` lines) and only says it in calendar terms. Each is a
derivation, so a calendar a page draws is composed from stored answers: what
the estate is due is one answer whatever window is asked, and what happened is
one per window.
"""

from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any

from django.utils import timezone

from hq.platform.application.routes import reverse

from .calendar import CalendarEvent, CalendarSource
from .derivations import derivation
from .derived_inputs import ESTATE_READS, estate_variant
from .estate import Expiry, estate_reading
from .ui import counted

# How many windows of one history source are kept current: the one the
# dashboard shows and the one last paged to.
WINDOWS = 2


def _whatever_window(first: date, last: date) -> tuple[Any, ...]:
    """What is due does not depend on the window asked: the calendar keeps
    what falls inside it."""

    return estate_variant()

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
            description="When each certificate you renew is due, and any that expired.",
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


def _certificate_event(expiry: Expiry) -> CalendarEvent:
    ends = _expiry_day(expiry)
    if expiry.managed:
        return CalendarEvent(
            id=f"certificate:{expiry.resource_key}",
            title=f"Renew {expiry.subject}",
            starts=ends - timedelta(days=expiry.renewal_window_days),
            ends=ends + timedelta(days=1),
            detail=f"Expires {expiry.phrase}",
            url=expiry.url,
        )
    return CalendarEvent(
        id=f"certificate:{expiry.source}:{expiry.subject}",
        title=f"{expiry.subject} expires",
        starts=ends,
        detail=f"{expiry.source} renews it and has not yet",
        url=expiry.url,
    )


@derivation("calendar.certificates", reads=ESTATE_READS, vary=_whatever_window)
def certificate_events(first: date, last: date) -> tuple[CalendarEvent, ...]:
    """Only what needs a person: a certificate its provider renews is silent.

    A managed certificate is its renewal window, opening the day renewal is
    due to start and ending the day it expires. A provider-renewed one that
    did not renew is its expiry.
    """

    return tuple(_certificate_event(expiry) for expiry in estate_reading().operator_certificates)


def _registration_event(expiry: Expiry) -> CalendarEvent:
    ends = _expiry_day(expiry)
    if expiry.auto_renew:
        return CalendarEvent(
            id=f"registration:{expiry.subject}",
            title=f"{expiry.subject} renews",
            starts=ends,
            detail="Auto-renews at the registrar",
            url=expiry.url,
        )
    return CalendarEvent(
        id=f"registration:{expiry.subject}",
        title=f"Renew {expiry.subject}",
        starts=ends - timedelta(days=REGISTRATION_NOTICE_DAYS),
        ends=ends + timedelta(days=1),
        detail=f"Registration expires {expiry.phrase}"
        + ("" if expiry.auto_renew is False else " · auto-renew not known"),
        url=expiry.url,
    )


@derivation("calendar.registrations", reads=ESTATE_READS, vary=_whatever_window)
def registration_events(first: date, last: date) -> tuple[CalendarEvent, ...]:
    """A registration that renews itself is a day; one that does not is a window."""

    return tuple(_registration_event(expiry) for expiry in estate_reading().registrations)


# ----- History ----------------------------------------------------------------


def history_sources() -> tuple[CalendarSource, ...]:
    """What happened, day by day. Unchecked until wanted: the day panel shows it."""

    return (
        CalendarSource(
            id="history.deploys",
            label="Deploys",
            events=deploy_events,
            description="Each deploy GitHub recorded.",
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
            label="Changes made in HQ",
            events=change_events,
            description="Changes made in HQ, and changes HQ noticed that were made elsewhere.",
            url=reverse("core:audit_list"),
            shown=False,
        ),
    )


def _moments(first: date, last: date, sources: set[str]):
    from .history import external

    since, until = _window(first, last)
    return external(sources, since=since, until=until)


# What the controller's readings hold: deploys, and when each container started.
_READINGS = ("control_plane.ProviderInventory",)


@derivation("calendar.deploys", reads=_READINGS, ahead=WINDOWS)
def deploy_events(first: date, last: date) -> tuple[CalendarEvent, ...]:
    from .history import DEPLOYS

    return tuple(
        CalendarEvent(
            id=f"deploy:{moment.url or moment.at.isoformat()}",
            title=moment.title,
            starts=moment.at,
            detail=moment.detail,
            url=moment.url,
            state="done",
        )
        for moment in _moments(first, last, {DEPLOYS})
    )


@derivation("calendar.containers", reads=_READINGS, ahead=WINDOWS)
def container_events(first: date, last: date) -> tuple[CalendarEvent, ...]:
    from .history import CONTAINERS

    return tuple(
        CalendarEvent(
            id=f"container:{moment.title}:{moment.at.isoformat()}",
            title=moment.title,
            starts=moment.at,
            detail=moment.detail,
            url=moment.url,
            state="done",
        )
        for moment in _moments(first, last, {CONTAINERS})
    )


@derivation("calendar.changes", reads=("core.AuditLog",), ahead=WINDOWS)
def change_events(first: date, last: date) -> tuple[CalendarEvent, ...]:
    """One line a day, by where the change was made: the day's history has the rest."""

    from .history import OUTSIDE_HQ, changes

    since, until = _window(first, last)
    rows = changes(since, until - timedelta(microseconds=1)).values_list("created_at", "source")
    per_day = Counter((timezone.localtime(at).date(), where) for at, where in rows)
    return tuple(
        CalendarEvent(
            id=f"changes:{where}:{day.isoformat()}",
            title=counted(
                count,
                "change made in HQ" if where != OUTSIDE_HQ else "change made elsewhere",
                "changes made in HQ" if where != OUTSIDE_HQ else "changes made elsewhere",
            ),
            starts=day,
            url=f"{reverse('core:audit_list')}?on={day.isoformat()}&source={where}",
            state="done",
        )
        for (day, where), count in sorted(per_day.items())
    )
