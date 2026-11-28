"""The browser layout gate's pages, each as its view draws it over the
crowded estate (``crowding``: the example estate with the crowding laid on
top), asked the way production asks.

Only what no view reads from the database is patched in: the dashboard's
domain contributors and its outward links, which come from installed
extensions, and the queue states a page is read in.
"""

import tempfile
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ApprovalRequest
from hq.platform.application.approvals import entry_event
from hq.platform.application.card_items import Kpi
from hq.platform.application.contracts import Effect
from hq.platform.application.drawings import Dot, Dots
from hq.platform.application.markup_testing import followed
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.links import example_command
from hq.platform.application.ui import ActivityCalendar, CalendarDay, ChartSeries, DomainOverview, stacked_bar_chart
from hq.platform.core.example_estate import REQUEST
from hq.platform.core.example_estate.records import SERVER_ASSET, UNFILED_RECEIPT
from hq.platform.core.models import AuditLog
from hq.platform.core.tests import crowding
from hq.platform.core.tests.browser.estate import ask_as_production
from hq.platform.core.tests.browser.gate import answered


def _highlights():
    series = (ChartSeries("Example series", (2, 5, 3, 6), 1),)
    chart = stacked_bar_chart(
        "Example movement",
        "A short description.",
        ("One", "Two", "Three", "Four"),
        series,
        unit="units",
    )
    calendar = ActivityCalendar(
        "Example calendar",
        "A deliberately taller six-week calendar.",
        tuple(
            tuple(
                CalendarDay(
                    date(2026, 1, 5) + timedelta(days=week * 7 + day),
                    "done",
                    (1,),
                    "Example activity",
                )
                for day in range(7)
            )
            for week in range(6)
        ),
        series=series,
        period_label="Example period",
    )
    short = tuple(Kpi(f"Reading {i}", str(i), url="/example/") for i in range(4))
    long = tuple(
        Kpi(
            f"Longer example reading {i}",
            str(i),
            "An intentionally longer reporting window and explanation.",
            "/example/",
        )
        for i in range(4)
    )
    # The most dots a figure draws, each a link a target apart, in a card that
    # shares its row and so sets its six figures three to a row.
    linked = tuple(
        Kpi(f"Linked {count}", str(count), "of them up", "/example/", drawing=_linked_dots(count)) for count in (12, 8)
    )
    dense = (*linked, Kpi("Plain", "4", url="/example/"), Kpi("Counted", "7", drawing=Dots.of(5, 7)), *short[:2])
    return {
        "highlights": [
            {
                "id": "example.dense",
                "label": "Example dense",
                "cards": [],
                "overview": DomainOverview("Example", "/example/", dense),
            },
            {
                "id": "example.first",
                "label": "Example one",
                "cards": [],
                "overview": DomainOverview("Example", "/example/", short),
            },
            {
                "id": "example.second",
                "label": "Example two",
                "cards": [],
                "overview": DomainOverview("Example", "/example/", long, (chart,), (calendar,)),
            },
        ],
        # A strip of counts as an installation fills it, some with a detail
        # after their words, wide enough to wrap on a phone.
        "compact": [
            {"label": "Example counts", "value": 3, "url": "/example/"},
            {"label": "Example open updates", "value": 2, "url": "/example/u/"},
            {"label": "Example new advisories", "value": 15, "url": "/example/a/"},
            {
                "label": "Example draft",
                "value": 1,
                "url": "/example/drafts/",
                "detail": "22 published",
            },
            {
                "label": "Example expenses",
                "value": "$1,204.50",
                "url": "/example/expenses/",
                "detail": "All deductible est.",
            },
        ],
    }


def _linked_dots(count):
    return Dots(tuple(Dot(filled=at % 3 != 0, tip=f"Example {at}", url=f"/example/{at}/") for at in range(count)))


_LINKS = [{"href": "https://example.com/", "label": f"Example link {i}", "sub": "example.com"} for i in range(24)]


@contextmanager
def _dashboard(*, populated):
    highlights = _highlights() if populated else {"highlights": [], "compact": []}
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch("hq.platform.core.dashboard_views.dashboard_highlights", return_value=highlights)
        )
        stack.enter_context(
            mock.patch(
                "hq.platform.core.dashboard_views.outward_links",
                return_value=(_LINKS if populated else [], None),
            )
        )
        if not populated:
            # Nothing contributes, the calendar's sources included.
            stack.enter_context(mock.patch("hq.platform.application.calendar.calendar_sources", return_value=()))
        yield


@contextmanager
def _urgent_dashboard():
    """The dashboard with a short urgent queue read in place: a held request
    and a finding, each with a long line, beside their buttons."""

    from dataclasses import asdict
    from uuid import UUID

    from hq.platform.application.approvals import DECISIONS, _decision_link
    from hq.platform.application.dashboard import finding_rows
    from hq.platform.application.finding_model import Finding, opens

    held = UUID(int=1)
    approval = Finding(
        rule="approval",
        subject=str(held),
        severity="serious",
        title="example-agent wants to change the expected answer of photos.example.com from 192.0.2.10 to 198.51.100.20",
        explanation="DNS rewrite photos-example: answer from 192.0.2.10 to 198.51.100.20, and 2 more fields.",
        primary=opens("", reverse("core:approval_entry", kwargs={"approval_id": held})),
        held=True,
    )
    finding = Finding(
        rule="example-expiry",
        subject="wildcard-example-com",
        severity="serious",
        title="wildcard-example-com expires in 4 days",
        explanation="Expires in 4 days. Renewal opens at 30 days; renew it or check why renewal did not run, since a "
        "certificate that lapses takes every name it covers down with it.",
    )
    # Each row with what its reader is offered: a held request's decisions
    # (``approvals.offers``), the finding's own remedy.
    offered = (
        [_decision_link(held, decision) for decision in DECISIONS],
        [example_command("renew", "Renew certificate", "remote_write", "/example/renew/", method="POST")],
    )
    queue = [
        {**row, "actions": [asdict(action) for action in actions], "aside": False}
        for row, actions in zip(finding_rows("example.urgent", "Example", (approval, finding)), offered, strict=True)
    ]
    with (
        _dashboard(populated=False),
        mock.patch("hq.platform.application.action_items.with_aside_state", return_value=queue),
    ):
        yield


@contextmanager
def _rechecks():
    """The queue as a machine's findings fill it: urgent rows whose only remedy
    is one press, beside the reader's Dismiss, each title a
    quoted unit name long enough to wrap. No row holds a third button, so a
    table of them is as narrow as two buttons leave it."""

    from dataclasses import asdict

    from hq.platform.application.dashboard import finding_rows
    from hq.platform.application.finding_model import Finding

    titles = (
        (
            "“Example Remote Upload Service” keeps restarting on example-host-01",
            "systemd starts it again each time it stops, and it does not stay up.",
        ),
        ("Edge certificate example.com expires in 19 days", "Visitors get a certificate error once it expires."),
    )
    found = tuple(
        Finding(rule="example-recheck", subject=str(place), severity="serious", title=title, explanation=detail)
        for place, (title, detail) in enumerate(titles)
    )
    queue = [
        {
            **row,
            "actions": [
                asdict(
                    example_command(
                        "remedy", "Restart it", Effect.REMOTE_WRITE, f"/example/restart/{place}/", method="POST"
                    )
                )
            ],
            "aside": False,
        }
        for place, row in enumerate(finding_rows("example.rechecks", "Example", found))
    ]
    with (
        _dashboard(populated=False),
        mock.patch("hq.platform.application.action_items.with_aside_state", return_value=queue),
    ):
        yield


def _expense() -> str:
    from hq.domains.expenses.models import Expense

    return Expense.objects.exclude(about="").first().get_absolute_url()


def _calendar_day() -> str:
    from hq.domains.calendars.models import Entry

    return Entry.objects.get(location__startswith="An example clinic").get_absolute_url()


def _held(capability) -> str:
    """The audit entry of the crowding agent's held request for ``capability``, where its decision card is."""

    held = ApprovalRequest.objects.get(capability=capability, requested_actor=crowding.AGENT)
    return reverse("core:audit_detail", args=[entry_event(held.pk).pk])


def _audit_event() -> str:
    changed = AuditLog.objects.get(object_repr=crowding.AUDITED, metadata__has_key="changes")
    return reverse("core:audit_detail", args=[changed.pk])


# name -> (url, context manager for what no view reads from the database[,
# the request's own fields])
PAGES = {
    "dashboard": (lambda: reverse("dashboard"), lambda: _dashboard(populated=True)),
    # The queue's urgent rows as a machine's findings fill it, each with two
    # buttons: in place on the dashboard, and on the queue's own page.
    "dashboard-rechecks": (lambda: reverse("dashboard"), _rechecks),
    "action-items-rechecks": (lambda: reverse("action_items"), _rechecks),
    # The urgent part of the queue read on the dashboard, beside its buttons.
    "dashboard-urgent": (lambda: reverse("dashboard"), _urgent_dashboard),
    "dashboard-bare": (lambda: reverse("dashboard"), lambda: _dashboard(populated=False)),
    "service": (lambda: reverse("control_plane:service", kwargs={"hostname": crowding.SERVICE}), ExitStack),
    "connections": (lambda: reverse("control_plane:connections"), ExitStack),
    "machines": (lambda: reverse("control_plane:machines"), ExitStack),
    "machine": (lambda: reverse("control_plane:machine", kwargs={"name": crowding.MACHINE}), ExitStack),
    "topology": (lambda: reverse("control_plane:topology"), ExitStack),
    "services": (lambda: reverse("control_plane:services"), ExitStack),
    "findings": (lambda: reverse("control_plane:findings"), ExitStack),
    "containers": (lambda: reverse("control_plane:containers"), ExitStack),
    "container": (lambda: reverse("control_plane:detail", kwargs={"key": "crowded-frontend"}), ExitStack),
    "resource": (lambda: reverse("control_plane:detail", kwargs={"key": "hq-proxy"}), ExitStack),
    "action-items": (lambda: reverse("action_items"), ExitStack),
    "projects": (lambda: reverse("projects:list"), ExitStack),
    # A command that changes infrastructure, so the page carries its consent
    # checkbox, which must not stretch across the row like a form field and
    # push its label out past the edge.
    "command-consent": (
        lambda: reverse("command", kwargs={"name": "infrastructure.controller.refresh"}),
        ExitStack,
    ),
    # A command that writes a record: selects, dates and fields side by side,
    # and a textarea taking its row.
    "command-form": (lambda: reverse("command", kwargs={"name": "asset.create"}), ExitStack),
    "connection": (lambda: reverse("connection"), ExitStack, {"HTTP_USER_AGENT": crowding.USER_AGENT}),
    # A band whose cells are cards: the frame rule must not zero their padding.
    "resource-kinds": (lambda: reverse("control_plane:create"), ExitStack),
    # A form laid out as a grid of fields, with a textarea taking the row.
    "project-form": (lambda: reverse("projects:create"), ExitStack),
    "calendar": (lambda: reverse("calendar:month"), ExitStack),
    "messages": (lambda: reverse("contacts:list"), ExitStack),
    # A day open beside the month, with an entry open in it.
    "calendar-day": (_calendar_day, ExitStack),
    # Records that name each other, and the form that picks what one names.
    "asset": (lambda: reverse("assets:detail", args=[SERVER_ASSET]), ExitStack),
    "expense": (_expense, ExitStack),
    "event-form": (lambda: reverse("calendar:entry_new"), ExitStack),
    "receipt-match": (lambda: reverse("receipts:match", args=[UNFILED_RECEIPT]), ExitStack),
    "starred": (lambda: reverse("watching"), ExitStack),
    "records": (lambda: reverse("control_plane:list"), ExitStack),
    "record": (lambda: reverse("control_plane:detail", kwargs={"key": crowding.OPERATED}), ExitStack),
    "controller": (lambda: reverse("control_plane:controller"), ExitStack),
    "tailnet": (lambda: reverse("control_plane:tailnet"), ExitStack),
    "audit": (lambda: reverse("core:audit_list"), ExitStack),
    # The held request's decision card, on the audit entry its link opens.
    "approval": (lambda: _held("documentation.sync"), ExitStack),
    # A held removal's card, which lists what approving it would take away.
    "removal": (lambda: _held("infrastructure.resource.remove"), ExitStack),
    # A held amendment that inserts one entry mid-list, said as that one entry.
    "amendment": (lambda: _held("infrastructure.resource.update"), ExitStack),
    "audit-event": (_audit_event, ExitStack),
}


def render_pages(client, pages=None):
    """Every page in ``pages`` (PAGES by default) as its view renders it for
    the owner signed in to ``client``, asked as production asks."""

    rendered = {}
    for name, (url, patches, *request) in (PAGES if pages is None else pages).items():
        with patches():
            response = ask_as_production(client, url(), **(request[0] if request else {}))
        if response.status_code != 200:
            raise AssertionError(f"{name} answered {response.status_code}")
        rendered[name] = answered(response)
    return rendered


def asked_on_opening(client):
    """The answers to the dashboard's ask, by the path the browser asks them
    at: asked for, and waiting on a controller that arrived a moment before.
    Every page that asks on opening hears the same, and draws what it says."""

    with tempfile.TemporaryDirectory() as directory:
        markers = Path(directory)
        (markers / "heartbeat").touch()
        with override_settings(
            SEVERINO_CONTROLLER_HEARTBEAT=str(markers / "heartbeat"),
            SEVERINO_CONTROLLER_DOORBELL=str(markers / "doorbell"),
            SEVERINO_ACTIVITY_MARKER=str(markers / "activity"),
        ):
            asked = client.post(
                reverse("control_plane:visit"),
                form_keyed({"subject": "page", "name": "dashboard"}),
                secure=True,
                headers={"X-Requested-With": "XMLHttpRequest"},
                **REQUEST,
            )
    # The control answers the press and the stream's asks for it alike, under way.
    if followed(asked) != ["estate"]:
        raise AssertionError(f"the dashboard's ask was answered {asked.content.decode()}")
    return {reverse("control_plane:visit").strip("/"): answered(asked)}
