"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. A new state of a shared part is
one entry here."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, override
from unittest import mock
from uuid import UUID

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.messages import ERROR, INFO, SUCCESS, WARNING
from django.contrib.messages.storage.base import Message
from django.core.paginator import Paginator
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.utils.html import escape

from hq.domains.jobs.models import Job, JobState
from hq.platform.application import calendar
from hq.platform.application.adoption import ONLY_READS, WriteGap
from hq.platform.application.appearance import Theme
from hq.platform.application.asks import DONE, FAILED, RUNNING, Ask, Standing
from hq.platform.application.cadence_chart import Cadence, CadenceMatrix, CadenceRow, CadenceWeek
from hq.platform.application.calendar import CalendarEvent, CalendarSource, SourceView, calendar_month
from hq.platform.application.card_items import Insight, Kpi, ListRow, Timeline, TimelineItem
from hq.platform.application.command_center import DiscoveryItem
from hq.platform.application.credential_mint import CredentialFix
from hq.platform.application.drawings import AllocationBar, Dot, Dots, Share, Trend
from hq.platform.application.entity_links import EntityLink
from hq.platform.application.finding_model import OperatorStep
from hq.platform.application.forms import ChoiceCards, SuffixedInput
from hq.platform.application.freshness import CURRENT, STALE, Freshness
from hq.platform.application.pages import Page, PageAction, PageVisit
from hq.platform.application.posture import CheckOutcome, CheckStanding, Posture, PostureCheck
from hq.platform.application.relationships import RelationGroup, Relationship, Relationships
from hq.platform.application.request_channel import NETWORK_CHANNEL, TAILNET_CHANNEL
from hq.platform.application.request_connection import Connection, Peering
from hq.platform.application.request_identity import Identity
from hq.platform.application.request_layers import Layer
from hq.platform.application.tables import TableSort
from hq.platform.application.tailnet import Device
from hq.platform.application.tailnet_presence import Presence
from hq.platform.application.ui import (
    ActivityCalendar,
    CalendarDay,
    ChartSeries,
    PageNavigation,
    PageSection,
    PlannedDay,
    PlanNote,
    WeekPlan,
    line_chart,
    stacked_bar_chart,
)
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, SPARSE_WIDTHS
lazy from hq.platform.application.dashboard import queue_item
lazy from hq.platform.application.path_model import Certificate, Hop, Route
lazy from hq.platform.application.request_path import CONTRADICTED, PROVEN, HopCheck
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets


@dataclass(frozen=True)
class Entry:
    """A part as a template writes it, with what it is given.

    ``premise`` is what the entry must draw to stand for its state, as a probe
    and its argument returning what falls short of it: a value meant to wrap
    that fits on one line holds nothing the survey could find wrong.
    ``heads_the_page`` says the part is a page's own head; any other is drawn
    under one, as every HQ page is. ``frame`` says the part is the page
    frame's own, which base.html draws beside the page's main rather than in
    it, ``"before"`` or ``"after"``, so it is drawn there, beside a main that
    holds a page head alone."""

    template: str
    context: dict[str, Any] = field(default_factory=dict)
    premise: tuple[str, Any] | None = None
    heads_the_page: bool = False
    frame: Literal["", "before", "after"] = ""
    # The address the part is drawn at, for a part that reads its request: a
    # page's query, or the row its address opens.
    address: str = ""
    # The settings the part is drawn under, for a part only some deployments draw.
    settings: dict[str, Any] = field(default_factory=dict)

    def drawn(self, name):
        with override_settings(**self.settings):
            head = "" if self.heads_the_page or self.frame else _head(name)
            context = _Asked(self.context, self.address) if self.address else Context(self.context)
            return head + Template(self.template).render(context)

    def page(self, name):
        """The entry on a page of its own: in its main, or beside it for a part of the frame."""

        drawn = self.drawn(name)
        return part_page(name, _head(name), **{self.frame: drawn}) if self.frame else part_page(name, drawn)


def _head(name):
    return render_to_string(HEAD, {"page": Page(title=name)})


class _Asked(Context):
    """A context drawn for a request at ``address``, which it carries as
    ``{% querystring %}`` and ``{% row_editor %}`` read it."""

    def __init__(self, values, address):
        self.request = RequestFactory().get(address)
        super().__init__({**values, "request": self.request})


HEAD = "partials/_page_head.html"


class _Drawn:
    """A template from its source, compiled when it is drawn, which a page
    head's ``page_aside`` includes as it would a template's name."""

    def __init__(self, source):
        self.source = source

    def render(self, context):
        return Template(self.source).render(context)


# A row of readings in each form a reading takes.
_READINGS = (
    "{% readings %}"
    '{% reading icon="network" url="/example/" modal="example" note_tone="attention" %}'
    '{% status "Example" tone="good" dot=True ink="context" %}'
    "{% lines %}<span>Example TLS 1.3</span>{% note %}An example check failed{% endreading %}"
    '{% reading icon="activity" url="/example/controller/" %}'
    '{% status "Examples" tone="attention" dot=True ink="context" %}'
    "{% lines %}<strong>Last read an example day ago</strong>{% endreading %}"
    "<div data-example-region>"
    '{% reading icon="server" id="example-host" tone="attention" title="Example containers" unit="containers" '
    'data_fragment_part="example-host" %}12'
    "{% lines %}<span>example-host-01</span><strong>2 example alerts</strong><span>CPU 4%</span>"
    "{% heading %}<strong>Example host</strong><span>An example summary</span>"
    '{% panel %}{% facts %}{% fact "Example CPU" value="4%" %}{% fact "Example memory" value="583 MB" %}{% endfacts %}'
    '{% foot %}<a href="/example/">Example containers</a><span><button type="button">Refresh</button>'
    '<a href="/example/settings/">Settings</a></span>{% endreading %}'
    '{% reading icon="cloud" id="example-weather" note_tone="stale" data_fragment_part="example-weather" %}71°'
    "{% lines %}<span>Example Mostly Cloudy</span><span>71–86°</span>"
    "{% note %}Last read an example 3 days ago · out of date"
    "{% heading %}<strong>Example weather</strong>"
    "{% panel %}<p>The last reading failed (ExampleError). This is the one before it.</p>"
    '{% foot %}<span><a href="/example/settings/">Settings</a></span>{% endreading %}'
    "</div>{% endreadings %}"
)
_READINGS_EMPTY = (
    "{% readings %}"
    '{% reading icon="activity" url="/example/controller/" %}'
    '{% status "Examples" dot=True ink="context" %}{% lines %}<span>Not configured</span>{% endreading %}'
    "<div data-example-region>"
    '{% reading icon="server" id="example-empty" data_fragment_part="example-empty" %}Example host'
    "{% lines %}<span>No reading yet</span>"
    "{% heading %}<strong>Example host</strong>{% panel %}<p>An example host has not been read yet.</p>{% endreading %}"
    '{% reading icon="cloud" id="example-long" data_fragment_part="example-long" %}71°'
    "{% lines %}<span>Example Mostly Cloudy with an example chance of showers later</span><span>71–86°</span>"
    "<span>Example wind 12 mph</span>"
    "{% heading %}<strong>Example weather</strong>{% panel %}<p>An example forecast.</p>{% endreading %}"
    "</div>{% endreadings %}"
)

# Each value that fits on one line of its own.
_ONE_LINE = """(values) => [...document.querySelectorAll(values)]
  .filter((value) => Math.round(value.getBoundingClientRect().height / parseFloat(getComputedStyle(value).lineHeight)) < 2)
  .map((value) => value.textContent.trim())"""


# A list opened to read more set no closer than the page's list before it.
_TIGHTER = """(lists) => {
  const [page, opened] = [...document.querySelectorAll(lists)].map((list) => parseFloat(getComputedStyle(list).rowGap));
  return opened < page ? [] : [`the opened list's rows are ${opened}px apart, the page's ${page}px`];
}"""


# Each form not drawn as one question, or drawn otherwise than its width
# calls for: its button on the control's line in a form of 30rem or more
# inside its padding, under the control in one narrower (forms.css).
_ONE_QUESTION = """({forms, question, control: controls, button: buttons}) => [...document.querySelectorAll(forms)].filter((form) => {
  if (!form.matches(question)) return true;
  const control = form.querySelector(controls).getBoundingClientRect();
  const button = form.querySelector(buttons).getBoundingClientRect();
  const style = getComputedStyle(form);
  const width = form.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const roomy = width >= 30 * parseFloat(getComputedStyle(document.documentElement).fontSize);
  return roomy ? !(button.top < control.bottom && button.bottom > control.top) : button.top < control.bottom;
}).map((form) => form.outerHTML.slice(0, 120))"""
_QUESTION_PARTS = {
    "forms": "main form",
    "question": SELECTORS["one_question"],
    "control": SELECTORS["question_control"],
    "button": SELECTORS["question_button"],
}

# Each drawing with no box of its own while the box that holds it has one.
_UNDRAWN = """(drawings) => [...document.querySelectorAll(drawings)].filter((drawing) => {
  const held = drawing.parentElement.getBoundingClientRect();
  const drawn = drawing.getBoundingClientRect();
  return held.width > 0 && held.height > 0 && (drawn.width < 1 || drawn.height < 1);
}).map((drawing) => drawing.outerHTML.slice(0, 120))"""


def _holding(entry: CalendarEvent) -> Callable[[date, date], tuple[CalendarEvent, ...]]:
    """A source's reader that holds ``entry`` alone."""

    return lambda _first, _last: (entry,)


def _month_gone_by():
    """A month seen from a day in it: each kind of entry on the days gone by,
    a bar running on past today, each in a series color whose fill sits
    closest to the ink a day gone by is set in."""

    month, today = date(2026, 1, 1), date(2026, 1, 21)
    entries = {
        7: CalendarEvent("example-bar", "Example window that runs past today", date(2026, 1, 12), date(2026, 1, 24)),
        1: CalendarEvent("example-timed", "Example timed line", datetime(2026, 1, 14, 15, tzinfo=UTC)),
        6: CalendarEvent("example-all-day", "Example all-day line", date(2026, 1, 15)),
        8: CalendarEvent("example-mark", "Example mark", date(2026, 1, 13), mark=True),
    }
    sources = tuple(
        ("Example", CalendarSource(f"example.series-{slot}", f"Example {slot}", _holding(entry), slot=slot))
        for slot, entry in entries.items()
    )
    with mock.patch.object(calendar, "calendar_sources", return_value=sources):
        return calendar_month(month, today=today)


_WEEK = date(2026, 1, 5)


class _ExampleForm(forms.Form):
    name = forms.CharField(label="Example name", help_text="An example of what the name is for.")
    notes = forms.CharField(label="Example notes", required=False, widget=forms.Textarea)


_EXAMPLE_ANSWERS = (
    ("refuse", "Refuse it", "Every example nobody listed is refused, which is what the example policy needs to work."),
    ("mark", "Mark it as suspicious", "Receivers usually accept it and flag it."),
    ("allow", "Allow it", "Every example passes."),
)


class _ExampleChoices(forms.Form):
    answer = forms.ChoiceField(
        label="Every other example",
        choices=[(value, label) for value, label, _ in _EXAMPLE_ANSWERS],
        widget=ChoiceCards(described={value: what for value, _, what in _EXAMPLE_ANSWERS}),
        help_text="What happens to an example the list above leaves out.",
    )
    share = forms.IntegerField(
        label="Apply it to", min_value=0, max_value=100, widget=SuffixedInput(suffix="%"), help_text="Of every example."
    )


class _ExampleRange(forms.Form):
    """A form of pairs (``starts_pair``): each range's start and end on a row
    of their own, whatever columns the form has, and a field after them."""

    starts_on = forms.DateField(label="Example start", widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(
        label="Example end", required=False, widget=forms.DateInput(attrs={"type": "date"}), help_text="An example end."
    )
    starts_at = forms.TimeField(label="Example from", required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    ends_at = forms.TimeField(label="Example to", required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    place = forms.CharField(label="Example place", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # An option field.html reads, which Django's Field does not declare.
        fields: dict[str, Any] = dict(self.fields)
        for name in ("starts_on", "starts_at"):
            fields[name].starts_pair = True


class _NoFields(forms.Form):
    """A command that takes nothing."""


class _ExampleLookup(forms.Form):
    name = forms.CharField(label="Example hostname", required=False)


class _ExampleNaming(forms.Form):
    name = forms.CharField(
        label="Example group", help_text="An example of what a group is named for, long enough to wrap on a phone."
    )


def _readings(title, lede, *names, unit="min"):
    """A line chart of a week's readings, one line for each name."""

    return line_chart(
        title,
        lede,
        tuple(
            (name, tuple((_WEEK + timedelta(days=day), float((day * slot) % 5 + slot)) for day in range(7)), slot)
            for slot, name in enumerate(names, 1)
        ),
        unit=unit,
    )


def _weeks(title, lede):
    """A stacked bar chart of four weeks, two kinds in each."""

    return stacked_bar_chart(
        title,
        lede,
        ("Week 1", "Week 2", "Week 3", "Week 4"),
        (ChartSeries("Running", (3, 4, 2, 5), 1), ChartSeries("Strength", (1, 2, 2, 1), 2)),
        unit="min",
    )


def _month():
    """A month of plan against what was done, paged, with its period."""

    return ActivityCalendar(
        "Example training",
        "Planned days against what was done.",
        tuple(
            tuple(
                CalendarDay(_WEEK + timedelta(days=week * 7 + day), "done", slots=(1,), planned=True)
                if day in (0, 2)
                else CalendarDay(_WEEK + timedelta(days=week * 7 + day), "missed", planned=True)
                if day == 4
                else CalendarDay(_WEEK + timedelta(days=week * 7 + day))
                for day in range(7)
            )
            for week in range(4)
        ),
        series=(ChartSeries("Example running", (), 1),),
        period_label="January 2026",
        previous_url="/example/?month=2025-12",
        next_url="/example/?month=2026-02",
        current_url="/example/",
    )


def _problems(*titles, family="", status="attention"):
    """The queue's items about a thing, each with its meaning and the check that reads again."""

    return [
        queue_item(
            "example",
            "Example",
            Insight(
                status=status,
                eyebrow="",
                title=title,
                value="",
                body="What it means for the thing this page is about, in one sentence.",
                key=f"example:{title}",
                family=family,
            ),
        )
        for title in titles
    ]


def _flow(*sections):
    """A run of sections in the flow (`.section-flow`), each named and holding
    what it is given."""

    return (
        '<div class="section-flow">'
        + "".join(f'{{% section "{heading}" %}}{body}{{% endsection %}}' for heading, body in sections)
        + "</div>"
    )


# A narrow section: a few facts, as the connection panel's identity draws them.
_NARROW = (
    '{% facts %}{% fact "Example name" value="example-01" %}{% fact "Example state" value="Online" %}{% endfacts %}'
)
# A wide one: a table of four columns, which takes the whole row.
_WIDE = (
    '{% table_region "Example hosts" %}<table class="data-table"><thead><tr>'
    '<th scope="col">Name</th><th scope="col">State</th><th scope="col">Address</th><th scope="col">Seen</th>'
    "</tr></thead><tbody><tr><td>example-01</td><td>Online</td><td>192.0.2.10</td><td>Today</td></tr></tbody>"
    "</table>{% endtable_region %}"
)


# What a part that says how long ago is drawn against.
_NOW = datetime.now(UTC)


def _matrix():
    """Three things over the same six weeks, one a link with a line about it."""

    def weeks(*marks):
        return tuple(
            CadenceWeek(label, hit=mark == "x", no_data=mark == "?", detail=f"Week of {label}")
            for label, mark in zip(("Jan 5", "Jan 12", "Jan 19", "Jan 26", "Feb 2", "Feb 9"), marks, strict=True)
        )

    return CadenceMatrix(
        ("Jan 5", "Jan 12", "Jan 19", "Jan 26", "Feb 2", "Feb 9"),
        (
            CadenceRow("Example run", weeks("x", "x", "-", "x", "x", "x"), url="/example/run/", detail="3 a week"),
            CadenceRow("Example lift", weeks("x", "-", "?", "x", "-", "x")),
            CadenceRow("An example habit with a name long enough to wrap", weeks("-", "-", "x", "x", "x", "-")),
        ),
    )


def _route():
    """A route as ``application.paths`` hands it to the strip: a hop in each
    state the strip draws, the first proven by the request."""

    return Route(
        via="example record",
        hops=(
            Hop("device", "Your device", name="example-laptop", check=HopCheck(PROVEN, "Example address 192.0.2.10.")),
            Hop("dns", "Internal DNS", name="app.example.com", check=HopCheck(PROVEN, "Example answer 192.0.2.20.")),
            Hop(
                "ingress",
                "Proxy",
                name="example-proxy-host-with-a-long-name.example.com",
                certificate=Certificate("Proxy", name="app.example.com", issuer="Example CA"),
                check=HopCheck(CONTRADICTED, "The example request came another way."),
            ),
            Hop(
                "machine",
                "Machine",
                link=EntityLink("example-host-01", "/example/host/", kind="machine", kind_label="machine"),
            ),
            Hop(
                "container",
                "Container",
                name="example-app",
                facts=(("Up 3 days", ""), ("0a1b2c3", "https://example.com/example/app/commit/0a1b2c3")),
            ),
            Hop("served", "Served", unread="the example controller has not read it"),
        ),
    )


@dataclass(frozen=True)
class _Service:
    """A row of the services table, as far as its name cell reads it."""

    hostname: str
    pinned: bool = False


class _ExampleFiles(forms.Form):
    exports = forms.FileField(label="Example exports", help_text="An example of the files it takes.")


def _runs():
    """The last six runs of a piece of work, oldest first, each leading to its run."""

    return Dots(
        tuple(
            Dot(filled=done, tip=f"Example run {index}: {'done' if done else 'failed'}", url=f"/example/runs/{index}/")
            for index, done in enumerate((True, True, False, True, False, True), 1)
        ),
        ordered=True,
    )


def _connection(channel=TAILNET_CHANNEL, *, path="direct", held=True, linked=True):
    """A request as the connection panel reads it: from an example laptop to
    an example HQ, over ``path`` (``direct``, ``relayed``, or ``""`` for no
    tailnet reading), its checks confirmed when ``held`` and one not yet
    read otherwise, each end leading to its machine when ``linked``."""

    laptop = Device("example-laptop", addresses=("192.0.2.10",), user="example-person@example.com")
    hq = Device("example-hq", addresses=("192.0.2.20",))
    presence = (
        Presence(
            public_key="example-key",
            last_handshake="2026-01-01T00:00:00Z",
            direct_endpoint="198.51.100.40:41641" if path == "direct" else "",
            relay="" if path == "direct" else "example-region",
        )
        if path
        else None
    )
    tailnet = channel is TAILNET_CHANNEL
    return Connection(
        address="192.0.2.10",
        channel=channel,
        device=laptop if tailnet else None,
        serves=hq if tailnet else None,
        observer=None,
        identity=Identity(username="example", tailnet_user=laptop.user if tailnet else ""),
        layers=(Layer("example-check", "Example check", holds=True, detail="An example check.", conclusive=held),),
        secure_transport=True,
        host="hq.example.com",
        machine_url="/example/machines/example-laptop/" if linked else "",
        serves_url="/example/machines/example-hq/" if linked else "",
        presence=presence,
    )


def _posture(met, *, advanced=True):
    """The estate's posture, one example check for each reading in ``met``:
    met, not met, or ``None`` for not read."""

    def standing(index, held):
        outcome = CheckOutcome(met=held, evidence="")
        check = PostureCheck(f"example-{index}", f"Example check {index}", "", "", "", "", read=lambda inputs: outcome)
        return CheckStanding(check, outcome)

    return Posture(advanced, tuple(standing(index, held) for index, held in enumerate(met, 1)))


def _paged(number):
    """A list's page ``number`` of three."""

    paginator = Paginator(range(60), 20)
    return {"is_paginated": True, "paginator": paginator, "page_obj": paginator.page(number)}


def _toolbar():
    """A list's table as its view hands it to the toolbar: words searched for,
    one filter with an option chosen, a toggle set and a sort."""

    return {
        "query": "example",
        "search_placeholder": "Search example hosts…",
        "filters": [
            {
                "name": "kind",
                "label": "Kind",
                "selected_count": 1,
                "options": [
                    {"value": "machine", "label": "Example machine", "selected": True},
                    {"value": "container", "label": "Example container", "selected": False},
                ],
            }
        ],
        "toggles": [{"name": "stale", "label": "Out of date only", "selected": True}],
        "sorts": (TableSort("name", "Name", "name"), TableSort("-seen", "Last seen", "-seen")),
        "selected_sort": "_relevance",
        "active_count": 3,
    }


class _ExampleDistance(forms.Form):
    distance_miles = forms.DecimalField(label="Example miles", min_value=0)


# A table whose rows each open an editor under them (`row_editor`).
_EDITED_ROWS = (
    '{% section "Example runs" %}{% table_region "Example runs" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Run" "identifier" gutter=True %}{% table_heading "Miles" "numeric" %}'
    '{% table_heading "Distance check" "control" %}</tr></thead><tbody>'
    '<tr><td>example-01</td><td>3.10</td><td>{% row_editor form "example-01" label="Correct distance" '
    'action="/example/distance/" verb="Save distance" distance_miles="3.10" %}</td></tr>'
    '<tr><td>example-02</td><td>5.00</td><td>{% row_editor form "example-02" label="Correct distance" '
    'action="/example/distance/" verb="Save distance" distance_miles="5.00" %}</td></tr>'
    "</tbody></table>{% endtable_region %}{% endsection %}"
)


# Each provider's last sweep, as ``inventory.inventory_state`` hands it over:
# one current, one out of date, one unreachable.
_INVENTORY = (
    {
        "kind": "example.records",
        "label": "Example records",
        "count": 12,
        "reachable": True,
        "error": "",
        "observed_at": _NOW - timedelta(minutes=5),
        "state": "readable",
        "state_label": "Readable",
        "refused_parts": (),
    },
    {
        "kind": "example.hosts",
        "label": "Example hosts",
        "count": 4,
        "reachable": True,
        "error": "",
        "observed_at": _NOW - timedelta(days=2),
        "state": "stale",
        "state_label": "Out of date",
        "refused_parts": ("Example addresses not read",),
    },
    {
        "kind": "example.zones",
        "label": "An example provider with a long name",
        "count": 0,
        "reachable": False,
        "error": "The example provider refused the credential.",
        "observed_at": None,
        "state": "refused",
        "state_label": "Refused",
        "refused_parts": (),
    },
)


def _signed_in():
    """What the page frame reads of a signed-in example operator's request,
    as the context processors hand it to base.html: the sections, a group of
    them among them, the theme and its choices, the connection's channel and
    the queue's count."""

    return {
        "SITE_NAME": "Example HQ",
        "user": get_user_model()(
            username="example-operator", first_name="Example", last_name="Operator", is_staff=True
        ),
        "nav_entries": (
            {"kind": "item", "label": "Dashboard", "url": "dashboard", "active": True},
            {
                "kind": "group",
                "label": "Example section",
                "active": False,
                "items": (
                    {"label": "Example search", "url": "search", "active": False},
                    {"label": "Example queue", "url": "action_items", "active": False},
                ),
            },
        ),
        "THEME": Theme.SYSTEM,
        "THEME_CHOICES": Theme.choices,
        "CONNECTION_CHANNEL": TAILNET_CHANNEL,
        "agents_paused": False,
        "profile_action_count": 3,
        "show_action_count": True,
    }


def _palette():
    """Command Center's results for a term, as the search page hands them to
    the palette: the estate first, a group of records found by their words,
    then the commands, with more matches than it shows."""

    def found(kind, label, summary, destination, *badges):
        return DiscoveryItem(kind, label, label, summary, "/example/", destination, badges)

    return {
        "q": "example",
        "palette_estate": [
            {
                "key": "estate",
                "label": "Machines, services and domains",
                "items": (
                    found("machine", "example-host-01", "An example machine", "Machine", "Online"),
                    found("service", "example.example.com", "An example service", "Service", "Public", "Proxied"),
                ),
            }
        ],
        "palette_search_groups": [
            {
                "scope": "projects",
                "label": "Projects",
                "items": (
                    {
                        "title": "Example project with a long descriptive name",
                        "badge": "example-01",
                        "url": "/example/",
                        "timestamp": datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
                        "snippet": (("An ", False), ("example", True), (" line the search found.", False)),
                    },
                ),
            }
        ],
        "palette_groups": [
            {
                "key": "commands",
                "label": "Commands",
                "items": (found("command", "Example restart", "Restart an example service", "Command"),),
            }
        ],
        "palette_count": 4,
        "palette_total": 9,
    }


CATALOG = {
    # The page frame's header, as every signed-in page draws it: the sections,
    # the doorway to search, the queue's count and the person's menu, whose
    # popovers the survey opens.
    "part/site-header": Entry(
        '{% include "partials/_site_header.html" %}', _signed_in(), address="/example/", frame="before"
    ),
    # A section's heading with a long name, the line under it, and states beside it.
    "part/section-head": Entry(
        "{% card %}"
        '{% section_head lede="An example lede, the line that says what the section holds." %}'
        "An example heading long enough to need the row"
        '{% aside %}<span class="pill pill-good">Example state</span>'
        '<span class="pill pill-neutral">Another example state</span>'
        "{% endsection_head %}<p>Example.</p>{% endcard %}"
    ),
    # A table in a section of its own name, one that fits and one that
    # scrolls in a card of its own name, as a record page and a ledger draw
    # them: each name is one landmark on the page.
    "part/section-around-table": Entry(
        '{% section "Example records" %}{% table_region "Example records" %}'
        '<table class="data-table"><tbody><tr><td>Example</td></tr></tbody></table>'
        "{% endtable_region %}{% endsection %}"
        '{% card heading="example-ledger" %}<h2 id="example-ledger">Example ledger</h2>'
        '{% table_region "Example ledger" pane=True %}'
        '<table class="data-table"><tbody><tr><td>Example</td></tr></tbody></table>'
        "{% endtable_region %}{% endcard %}"
    ),
    # Sibling sections in the flow: two, three and four narrow ones share a
    # row as far as the box holds them, and a wide one, written ahead of the
    # narrow ones, takes the row before they share theirs.
    "part/flow-two": Entry(_flow(("Example identity", _NARROW), ("Example link", _NARROW))),
    "part/flow-three": Entry(
        _flow(("Example identity", _NARROW), ("Example link", _NARROW), ("Example path", _NARROW))
    ),
    "part/flow-four": Entry(
        _flow(
            ("Example identity", _NARROW),
            ("Example link", _NARROW),
            ("Example path", _NARROW),
            ("Example device", _NARROW),
        )
    ),
    "part/flow-wide": Entry(_flow(("Example hosts", _WIDE), ("Example identity", _NARROW), ("Example link", _NARROW))),
    # The section part ({% section %}) in each state a page can give it; a new
    # option of the tag comes with an entry here (test_catalog's source check).
    "part/section-bare": Entry(
        '{% section "Example notes" %}<p>An example line of prose in the frame.</p>{% endsection %}'
    ),
    "part/section-full": Entry(
        '{% section "An example heading long enough to need the row" count="12 example hosts" id="example-hosts"'
        ' css="example" data_example="example"'
        ' lede="An example lede, the line that says what the section holds." %}'
        "<p>An example line of prose in the frame.</p>"
        '{% aside %}<span class="pill pill-neutral">Example state</span>'
        ' <a class="btn ghost compact" href="/example/">Example action</a>{% endsection %}'
    ),
    # Reference text behind an info button in a section's body (partials/_about.html).
    "part/about": Entry(
        '{% section "Example endpoint" %}<p>An example line of prose in the frame.</p>'
        '{% include "partials/_about.html" with id="example-about" title="Example notes"'
        ' lede="An example lede, the line under the dialog\'s title." body_template="partials/_empty_state.html"'
        ' message="An example paragraph of reference text." %}{% endsection %}'
    ),
    "part/section-empty": Entry(
        '{% section "Example agents" count=0 %}{% empty %}No example agent has connected yet.{% endsection %}'
    ),
    # An empty line with the link that fills it, as a record with no receipt draws it.
    "part/section-empty-remedy": Entry(
        '{% section "Example receipts" remedy_label="Upload an example receipt" remedy_url="/example/new/" %}'
        "{% empty %}No example receipts.{% endsection %}"
    ),
    # A heading that is the moment it names, as a calendar's month is, with
    # its controls beside it; and a moment after the heading's words, as a
    # calendar's day is.
    "part/section-moment": Entry(
        '{% section moment=month when="month" id="example-month" %}<p>An example line of prose in the frame.</p>'
        '{% aside %}<a class="btn ghost compact" href="/example/">Example action</a>{% endsection %}'
        '{% section "Example day" moment=month when="day" %}<p>An example line of prose in the frame.</p>'
        "{% endsection %}",
        {"month": date(2026, 11, 1)},
    ),
    # A table whose region carries the heading's name, as a list's sections draw one.
    "part/section-table": Entry(
        '{% section "Example hosts" count=2 %}{% table_region "Example hosts" %}'
        '<table class="data-table"><thead><tr><th scope="col">Name</th><th scope="col">State</th></tr></thead>'
        "<tbody><tr><td>example-01</td><td>Online</td></tr><tr><td>example-02</td><td>Online</td></tr></tbody>"
        "</table>{% endtable_region %}{% aside %}<span>Example source</span>{% endsection %}"
    ),
    # A thing's problems inside one of its sections: the queue row's dense form,
    # one line each, then the section's own table.
    "part/queue-dense": Entry(
        '{% section "Example containers" %}{% include "partials/_problems.html#rest" with rest=rest %}'
        "<p>The section's own content.</p>{% endsection %}",
        {"rest": _problems("example-web-01 runs an image by tag alone", "example-worker-02 restarts often")},
    ),
    # Several of one family: one row saying what kind and how many, opening to them.
    "part/queue-family": Entry(
        '{% section "Example containers" %}{% include "partials/_problems.html#rest" with rest=rest %}{% endsection %}',
        {
            "rest": _problems(
                "example-web-01 runs an image by tag alone",
                "example-web-02 runs an image by tag alone",
                "example-web-03 runs an image by tag alone",
                family="Example images by tag",
            )
        },
    ),
    # A list whose rows carry their own padding fills the frame as a table does.
    "part/section-rows": Entry(
        '{% section "Example repositories" count=2 %}{% include "partials/_record_list.html" %}{% endsection %}',
        {
            "rows": [
                ListRow("example-one", url="/example/one/", detail="Example access, read only"),
                ListRow("example-two", url="/example/two/", detail="Example access, read only"),
            ]
        },
    ),
    # A band of facts, which draws its own cells, fills the frame as a table
    # does, the prose after it in the frame's padding; as a project's GitHub
    # panel draws one.
    "part/section-band": Entry(
        '{% section "Example repository" %}{% facts layout="band" %}'
        '{% fact "Example commit" value="Example" detail="An example detail" icon="commit" %}'
        '{% fact "Example checks" value="Passing" detail="An example detail" icon="check-circle" tone="good" %}'
        '{% fact "Example alerts" value="Clear" icon="shield" %}'
        "{% endfacts %}<p>An example line after the band.</p>{% endsection %}"
    ),
    "part/section-fold": Entry(
        '{% section "Example readings" count=3 fold=True lede="An example lede of a folded section."'
        " %}<p>An example line behind the fold.</p>{% aside %}<span>Example source</span>{% endsection %}"
    ),
    "part/section-fold-open": Entry(
        '{% section "Example names" fold=True open=True %}{% table_region "Example names" %}'
        '<table class="data-table"><thead><tr><th scope="col">Name</th></tr></thead>'
        "<tbody><tr><td>example.com</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
    ),
    "part/section-head-above": Entry('{% section "Example" head="above" %}<p>An example line.</p>{% endsection %}'),
    "part/section-head-inside": Entry(
        '{% section "Example" head="inside" %}{% table_region "Example" %}<table class="data-table"><thead><tr>'
        "<th>Name</th></tr></thead><tbody><tr><td>Example</td></tr></tbody></table>{% endtable_region %}"
        "{% endsection %}"
    ),
    # A line under a body that says how to read it, after a table that would
    # otherwise end the frame, and the aside's link to the whole, after a
    # line of its own and alone.
    "part/section-foot": Entry(
        '{% section "Example trends" more_label="All example trends" more_url="/example/" %}'
        '{% table_region "Example trends" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Example" "identifier" %}{% table_heading "Slope" "numeric" %}</tr></thead>'
        "<tbody><tr><td>Example pace</td><td>0.4</td></tr></tbody></table>{% endtable_region %}"
        '{% aside %}<span class="count">2 example weeks</span>'
        "{% foot %}R² is how much of the example movement the fitted line explains. It does not show cause."
        "{% endsection %}"
    ),
    "part/section-more": Entry(
        '{% section "Example renewals" count=1 more_label="View example renewals" more_url="/example/" %}'
        "<p>An example line of prose in the frame.</p>{% endsection %}"
    ),
    # A drawing as a figure, its head the caption: the heading, the line that
    # says what it shows, and its legend beside them.
    "part/section-figure": Entry(
        '{% section "Example allocation" figure=True lede="Each example account as its share of the whole." %}'
        '{% include "partials/_allocation.html" %}'
        '{% aside %}<span class="count">2 example accounts</span>{% endsection %}',
        {"allocation": AllocationBar("Example allocation", (Share("Cash", 3, 1), Share("Owed", 1, 2)))},
    ),
    # The bar with its legend, as a card of a whole and its parts draws it: each part's
    # swatch, amount (debt in its tone), label and share, each a link onward.
    "part/allocation-legend": Entry(
        '{% section "Example net worth" figure=True %}'
        '{% include "partials/_allocation.html" with legend=True stacked=True %}'
        "{% endsection %}",
        {
            "allocation": AllocationBar(
                "Example assets by kind",
                (
                    Share("Cash", 4200, 1, url="/example/?type=cash", detail="$4,200.00"),
                    Share("Investments", 12800, 3, url="/example/?type=investment", detail="$12,800.00"),
                    Share("Owed", 1500, 2, url="/example/?type=owed", detail="-$1,500.00", tone="attention"),
                ),
            )
        },
    ),
    "part/section-fold-empty": Entry(
        '{% section "Example dismissed" fold=True open=True %}{% empty %}Nothing example is dismissed.{% endsection %}'
    ),
    # Sections in a section, as a project's GitHub panel draws them: each inner
    # head quieter than the outer one, and each spaced from what is above it
    # by the stack gap, after prose and after a list of rows alike.
    "part/section-nested-rows": Entry(
        '{% section "Example repository" %}<p>An example line about the repository.</p>'
        '{% section "Example pull requests" count=2 %}{% include "partials/_record_list.html" %}{% endsection %}'
        '{% section "Example workflows" %}<p>Example build, example test</p>{% endsection %}'
        "{% endsection %}",
        {
            "rows": [
                ListRow("Example change", url="/example/41/", detail="#41"),
                ListRow("Another example change", url="/example/42/", detail="#42", badge="Draft"),
            ]
        },
    ),
    # A section in a section: the inner head in the outer frame, the inner
    # table reaching the outer frame's sides and foot.
    "part/section-nested": Entry(
        '{% section "Example path" %}<p>An example line before the steps.</p>'
        '{% section "Example steps" %}{% table_region "Example steps" %}'
        '<table class="data-table"><thead><tr><th scope="col">Step</th><th scope="col">State</th></tr></thead>'
        "<tbody><tr><td>Example DNS</td><td>Working</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
        "{% endsection %}"
    ),
    # A section inside a card, as a topology node or a held request's preview
    # draws it: part of the card's frame, its head with no rule or inset.
    "part/section-in-card": Entry(
        "{% card %}<p>An example line about the card.</p>"
        '{% section "Example preview" count=2 %}<p>An example line in the inner section.</p>'
        '{% aside %}<span class="pill pill-neutral">Example state</span>{% endsection %}{% endcard %}'
    ),
    # A reading as a folded section, open: its long lines wrap in the frame and
    # only its height scrolls.
    "part/section-readout": Entry(
        '{% section "Example readings" %}{% section "Example reading" fold=True open=True %}'
        '<pre tabindex="0">{"example_key_with_a_long_name": "an-example-value-long-enough-to-wrap-at-a-phone-width",'
        ' "another_example_key": "another-example-value"}</pre>{% endsection %}{% endsection %}'
    ),
    # A link onward whose words wrap at a phone's width: its arrow stays with
    # its last word.
    "part/section-more-wrapped": Entry(
        '{% section "Example request" more_label="example-service-with-a-deliberately-long-name.example.com"'
        ' more_url="/example/" %}<p>An example line of prose in the frame.</p>{% endsection %}'
    ),
    # A trail of steps a word of two letters long: each is a small target, so
    # the survey's small targets hold it by its spacing from the next, the
    # slash between them belonging to neither.
    "part/short-trail": Entry(
        f'{{% include "{HEAD}" %}}',
        {
            "page": Page(
                title="Example",
                trail=(("Ex", "/example/"), ("Ex", "/example/ex/"), ("Example", "/example/ex/example/")),
            )
        },
        heads_the_page=True,
    ),
    # A range beside its unit, and a phrase, in a cell held narrow, as three
    # figures to a row on a phone hold it: each wraps, its lines clear of each other.
    "part/wrapped-figures": Entry(
        '<div style="inline-size: 110px">{% include "partials/_kpi_grid.html" %}</div>',
        {"items": [Kpi("Example range", "10.5–12.5 mi"), Kpi("Example phrase", "up to 2 a week")]},
        premise=(_ONE_LINE, SELECTORS["figure_value"]),
    ),
    # A domain's overview as the dashboard draws it, its lead figure with the
    # line of its recent readings beside it.
    "part/figure-trend": Entry(
        '<div class="section-flow">{% section "Example overview" %}'
        '{% include "partials/_kpi_grid.html" with featured=True %}{% endsection %}</div>',
        {
            "items": [
                Kpi(
                    "Example balance", "$12,480", "Across every example account", drawing=Trend((9.0, 11.0, 10.0, 12.5))
                ),
                Kpi("Example income", "$4,200"),
                Kpi("Example spending", "$3,100"),
                Kpi("Example saved", "26%"),
            ]
        },
    ),
    # A whole's figure as a section leads with it, the line under it saying
    # how it moved in its tone, its parts under it with their legend, and
    # the changes beside it each in the tone of the way it went.
    "part/figure-toned": Entry(
        '{% section "Example net worth" %}{% include "partials/_kpi_grid.html" with featured=True %}{% endsection %}',
        {
            "items": [
                Kpi(
                    "Example net worth",
                    "$15,500.00",
                    "+$1,240.00 in the last 30 days",
                    url="/example/accounts/",
                    detail_tone="good",
                    drawing=AllocationBar(
                        "Example assets by kind",
                        (
                            Share("Cash", 4200, 1, url="/example/?type=cash", detail="$4,200.00"),
                            Share("Investments", 12800, 3, url="/example/?type=investment", detail="$12,800.00"),
                            Share("Owed", 1500, 2, url="/example/?type=owed", detail="-$1,500.00", tone="attention"),
                        ),
                    ),
                ),
                Kpi("Since yesterday", "+$120.00", tone="good"),
                Kpi("Available credit", "$8,000.00", tone="neutral"),
                Kpi("Unrealized gain", "−$40.00", tone="attention"),
            ]
        },
    ),
    # Figures in a row of their own, as a report's year draws them: a line
    # that counts records leading to them, a figure that is its page's link,
    # one at zero, and a mark before a label.
    "part/figure-linked-line": Entry(
        '{% include "partials/_kpi_grid.html" %}',
        {
            "items": [
                Kpi("Example expenses", "$1,204.50", "12 example expenses", detail_url="/example/expenses/"),
                Kpi("Example docs due", 3, url="/example/docs/", icon="page"),
                Kpi("Example assets", 0, "$0.00 total", is_zero=True),
            ]
        },
    ),
    # A list of records, each row's actions ending its line: one that cannot
    # be pressed, glyphs named by their titles, and a word; a row with a
    # state, a line of meta and a day, and one leaving HQ.
    "part/record-list-actions": Entry(
        '{% section "Example groups" %}{% include "partials/_record_list.html" %}{% endsection %}',
        {
            "rows": [
                ListRow(
                    "Example savings",
                    detail="3 example accounts",
                    actions=(
                        PageAction("↑", "/example/group/", method="post", value="up", title="Move up", disabled=True),
                        PageAction("↓", "/example/group/", method="post", value="down", title="Move down"),
                        PageAction("Remove", "/example/group/", method="post", value="delete", danger=True),
                    ),
                ),
                ListRow(
                    "Example checking",
                    url="/example/checking/",
                    status="attention",
                    badge="Needs review",
                    meta="2 example accounts",
                    at=date(2026, 11, 2),
                ),
                ListRow("Example docs", url="https://example.com/", external=True),
            ]
        },
    ),
    # Rows named by the things they link, as a record's related lists and
    # the dashboard's projects draw them: a note, a day, a long name, and a
    # project's category as its color.
    "part/record-list-links": Entry(
        '{% section "Example related" %}{% include "partials/_record_list.html" %}{% endsection %}',
        {
            "rows": [
                ListRow(
                    link=EntityLink("example-host-one", "/example/host/", kind="machine", kind_label="machine"),
                    meta="An example note",
                ),
                ListRow(
                    link=EntityLink(
                        "An example document with a title long enough to wrap",
                        "/example/document/",
                        kind="document",
                        kind_label="document",
                    ),
                    detail="Example category",
                    at=date(2026, 11, 2),
                ),
                ListRow(
                    link=EntityLink("Example project", "/example/project/", kind="project", kind_label="project"),
                    detail="Homelab",
                    category="homelab",
                    at=date(2026, 11, 3),
                ),
            ]
        },
    ),
    # Search hits, each with where the record matched under its line: a
    # match inside a word, a snippet long enough to be cut at its second
    # line, and record text that is not HQ's words.
    "part/record-list-snippets": Entry(
        '{% section "Example results" count=3 %}{% include "partials/_record_list.html" %}{% endsection %}',
        {
            "rows": [
                ListRow(
                    "Example backup host",
                    url="/example/host/",
                    detail="Machine",
                    at=date(2026, 11, 2),
                    snippet=(("Runs the nightly ", False), ("backup", True), ("s for example-host-one.", False)),
                ),
                ListRow(
                    "An example note with a long title that wraps on a phone",
                    url="/example/note/",
                    detail="Note",
                    snippet=(
                        ("The example ", False),
                        ("backup", True),
                        (
                            (
                                " runs at 02:00 and copies every example volume to the example store, then checks"
                                " each copy against its source and writes what it found to the example log, which"
                                " the morning report reads before anyone is awake to read it."
                            ),
                            False,
                        ),
                    ),
                ),
                ListRow(
                    "Example audit entry",
                    url="/example/audit/",
                    snippet=(("enabled: False to True at 2026-11-02 09:30, ", False), ("backup", True)),
                ),
            ]
        },
    ),
    # An absence said where a value would stand, beside a value that is
    # there, in a list of facts and in a table's cell.
    "part/empty-inline": Entry(
        '{% section "Example runs on" %}{% facts %}'
        '{% fact "Example container" %}{% include "partials/_empty_state.html" with message="No example container is known." inline=True %}{% endfact %}'
        '{% fact "Example image" value="example-1.2.3" %}'
        "{% endfacts %}{% endsection %}"
    ),
    # Steps done in order, each numbered: said in words, and a command to run
    # with its notes, as a credential's fix draws one.
    "part/steps-numbered": Entry(
        '{% section "Example upgrade" %}{% steps numbered=True %}'
        '{% step %}<strong>Pull the example image</strong> <span class="muted">An example sentence that says what the step does.</span>{% endstep %}'
        '{% include "partials/_operator_step.html" %}'
        "{% step %}<strong>Check the example</strong>{% endstep %}"
        "{% endsteps %}{% endsection %}",
        {
            "step": OperatorStep(
                label="Make a new example token on your Mac",
                command="example-mint --account example-01 --item example-token --permission example.read",
                notes=("HQ could not tell which example account holds it.", "Then press Refresh."),
            )
        },
    ),
    # A connection whose credential lacks permissions: its headline in the
    # row, its fix opened beneath it.
    "part/credential-fix": Entry(
        '{% section "Example connections" %}{% table_region "Example connections" %}<table class="data-table">'
        '<thead><tr>{% table_heading "Name" "identifier" gutter=True %}{% table_heading "Provider" %}</tr></thead>'
        '<tbody><tr><td>example-main{% include "control_plane/_credential_fix.html" %}</td><td>Example</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "fix": CredentialFix(
                connection_ref="example-main",
                provider="example",
                missing=("example.read", "example.write"),
                command="example-mint --account example-01 --item example-token",
                by_hand="the example provider's console",
            )
        },
    ),
    # Steps and checks side by side: met, unmet, done and plain.
    "part/steps": Entry(
        '{% section "Example standard" %}{% steps %}'
        '{% step icon="check-circle" tone="good" title="An example check holds." %}<strong>Example met</strong> <span class="muted">Stops an example.</span>{% endstep %}'
        '{% step icon="shield" tone="attention" %}<strong>Example unmet</strong> <span class="muted">An example is open: <a href="/example/">close it.</a> Stops an example.</span>{% endstep %}'
        '{% step icon="check-circle" done=True %}<strong>Example done</strong> <span class="muted">Done.</span>{% endstep %}'
        '{% step icon="card" %}<strong>Example step</strong> <span class="muted">An example sentence that says what the step is for.</span>{% endstep %}'
        "{% endsteps %}{% endsection %}"
    ),
    # A stack list ({% stack_list %}): a status with its words, one whose
    # link stands alone, and a note; then in a table, a row's notes under its name and a list
    # the open row shows under its label. A new option of the tag comes with
    # an entry here (test_stack_tags).
    "part/stack-list": Entry(
        '{% section "Example advisories" %}{% stack_list data_example="example" %}'
        '<li class="prose">{% status "High" tone="serious" %} <a href="/example/advisory/">EXAMPLE-2026-0001</a> '
        "An example advisory long enough to wrap onto a second line on a phone.</li>"
        '<li>{% status "Low" tone="attention" %} <a href="/example/advisory/2/">EXAMPLE-2026-0002</a></li>'
        '<li class="muted">and 2 more on its page</li>'
        "{% endstack_list %}{% endsection %}"
    ),
    "part/stack-list-in-a-row": Entry(
        '{% section "Example checks" %}{% table_region "Example checks" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Check" gutter=True %}{% table_heading "Result" %}</tr></thead><tbody>'
        "<tr><td>Example reading"
        '{% stack_list %}<li class="muted">An example reason it is not read.</li>'
        '<li class="muted">Another example reason.</li>{% endstack_list %}</td>'
        '<td class="cell-data">{% status "Failed" tone="serious" %}<br><strong>2 example names differ</strong>'
        '{% stack_list below="Example affected" %}<li><strong>example-proxy</strong>{% dot %}example.com</li>'
        "<li><strong>example-mail</strong>{% dot %}mail.example.com</li>{% endstack_list %}</td></tr>"
        "</tbody></table>{% endtable_region %}{% endsection %}"
    ),
    # A calendar's sources, each a toggle in its color: one shown with what it
    # shows, one hidden, one that could not be read; and a source's swatch.
    "part/calendar-sources": Entry(
        '{% section "Example calendars" %}{% include "partials/_calendar_sources.html" %}{% endsection %}'
        '{% section "Example day" %}<p>An example line.</p>{% aside %}'
        '{% include "partials/_calendar_sources.html#swatch" with source=sources.0 %}{% endsection %}',
        {
            "sources": (
                SourceView(
                    "example.shown",
                    "Example shown",
                    group="",
                    slot=1,
                    shown=True,
                    description="Every example event, as it is planned.",
                ),
                SourceView(
                    "example.hidden",
                    "Example hidden",
                    group="",
                    slot=4,
                    shown=False,
                    description="Each example that ran.",
                ),
                SourceView(
                    "example.failed",
                    "Example unread",
                    group="",
                    slot=7,
                    shown=True,
                    failure="Example source did not answer in 5 s.",
                ),
            )
        },
    ),
    # A month with days gone by, each kind of entry on them: their words are
    # still links a reader follows, so they read as text in play does.
    "part/month-gone-by": Entry(
        '{% section "Example month" %}{% include "calendars/_month_grid.html" %}{% endsection %}',
        {"calendar": _month_gone_by()},
    ),
    # The same month compact, as the dashboard draws it beside other sections:
    # where things are, each a dot or a sliver, the day opening the rest.
    "part/month-compact": Entry(
        '<div class="section-flow">{% section "Example month" %}'
        '{% include "calendars/_month_grid.html" with compact=True %}{% endsection %}'
        '{% section "Example notes" %}<p>An example line of prose in the frame.</p>{% endsection %}</div>',
        {"calendar": _month_gone_by()},
    ),
    # A month as the calendar page draws it, the main of a `.split` beside a
    # flow of sections: the month takes the room, the sections keep their
    # basis beside it and pair up once they wrap under it.
    "part/month-split": Entry(
        '<div class="split">{% section "Example month" %}{% include "calendars/_month_grid.html" %}'
        '{% endsection %}<aside class="section-flow">{% section "Example identity" %}' + _NARROW + "{% endsection %}"
        '{% section "Example link" %}' + _NARROW + "{% endsection %}</aside></div>",
        {"calendar": _month_gone_by()},
    ),
    # What the table region derives from its headings, each in a table of its
    # own: a figure's column aligned under its heading, body and totals, with
    # no class on a cell; the sort control on a table of rows and none on a
    # table of one; each row's toggle in the gutter column's cell, shown on a
    # row that holds something back.
    "table/figures": Entry(
        '{% section "Example costs" %}{% table_region "Example costs" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "Count" "numeric" %}{% table_heading "Cost" "numeric" %}'
        "</tr></thead><tbody><tr><td>example-01</td><td>3</td><td>$1,204.50</td></tr>"
        "<tr><td>example-02</td><td>12</td><td>$8.00</td></tr></tbody>"
        "<tfoot><tr><td>Total</td><td>15</td><td>$1,212.50</td></tr></tfoot></table>"
        "{% endtable_region %}{% endsection %}"
    ),
    "table/sorts": Entry(
        '{% section "Example hosts" %}{% table_region "Example hosts" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}'
        '{% table_heading "Actions" "control" hidden=True %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td><td><a href="#">Open</a></td></tr>'
        '<tr><td>example-02</td><td>{% status "Offline" tone="attention" %}</td><td><a href="#">Open</a></td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}"
        '{% section "Example host" %}{% table_region "Example host" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td></tr></tbody></table>'
        "{% endtable_region %}{% endsection %}"
    ),
    # A region whose tables hold no row: what is missing and what fills it,
    # then a list its own filter emptied, with the way back.
    "table/empty": Entry(
        '{% section "Example hosts" %}{% table_region "Example hosts" empty="No example hosts yet." '
        'action_url="#" action_label="Add an example host" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead><tbody></tbody></table>'
        "{% endtable_region %}{% endsection %}"
        '{% section "Example records" %}{% table_region "Example records" empty="No example records yet." '
        'filtered=True clear="#" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead><tbody></tbody></table>'
        "{% endtable_region %}{% endsection %}"
    ),
    "table/gutter": Entry(
        '{% section "Example records" %}{% table_region "Example records" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" gutter=True %}{% table_heading "Note" %}</tr></thead><tbody>'
        "<tr><td><strong>example-01</strong></td><td>An example note"
        '<div class="row-below">An example line the row holds back.</div></td></tr>'
        "<tr><td><strong>example-02</strong></td><td>Another example note</td></tr></tbody></table>"
        "{% endtable_region %}{% endsection %}"
    ),
    # A row's actions ({% row_actions %}): a POST and a destructive link side
    # by side, a link alone, one refused, and a row whose note takes several
    # lines, each set on its row's first line.
    "table/row-actions": Entry(
        '{% section "Example messages" %}{% table_region "Example messages" %}<table class="data-table"><thead><tr>'
        '{% table_heading "From" "identifier" gutter=True %}{% table_heading "Note" %}'
        '{% table_heading "Actions" css="actions-col" hidden=True drop=1 %}</tr></thead><tbody>'
        "{% for row in rows %}<tr><td><strong>{{ row.0 }}</strong></td><td>{{ row.1 }}</td>"
        "{% row_actions row.2 %}</tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "rows": (
                (
                    "example-01",
                    "An example note.",
                    (
                        PageAction("Read", "/example/1/status/", method="post", value="read", field="status"),
                        PageAction("Delete", "/example/1/delete/", danger=True),
                    ),
                ),
                ("example-02", "Another example note.", (PageAction("Open", "/example/2/"),)),
                (
                    "example-03",
                    "An example note that wraps on a phone.",
                    (PageAction("Spam", "/example/3/", method="post", disabled=True, title="An example reason."),),
                ),
            )
        },
    ),
    # A status as its words in its tone, among figures, as a change is drawn.
    "status/toned": Entry(
        '{% section "Example changes" %}<p>{% status "+$120.00" tone="good" pill=False %}'
        ' {% status "−$40.00" tone="attention" pill=False %} {% status "−$900.00" tone="serious" pill=False %}'
        ' {% status "$0.00" tone="neutral" pill=False %}</p>{% endsection %}'
    ),
    # A standing state after its dot: its words in its tone, then a heading's
    # own words in the ink around them, the dot alone toned.
    "status/dot": Entry(
        '{% section "Example states" %}<p>{% status "Allowed" tone="good" dot=True %}'
        ' {% status "Paused" tone="attention" dot=True %} {% status "Refused" tone="serious" dot=True %}'
        ' {% status "Not tested" tone="neutral" dot=True %}</p>'
        '<p><strong>{% status "Example" tone="good" dot=True ink="context" %}</strong>'
        ' {% status "Examples" tone="attention" dot=True ink="context" %}</p>{% endsection %}'
    ),
    # The names a record carries, as chips beside its name: each as written.
    "status/chip": Entry(
        '{% section "Example project" %}<p><strong>example-app</strong> {% status "django" chip=True %}'
        '{% status "example.com" chip=True %}{% status "Reverse proxy" chip=True title="An example role" %}</p>'
        "{% endsection %}"
    ),
    # Each chart partial, its head a section's: the heading, the line under
    # it, and its legend or a month's controls beside them; its data a folded
    # section in its frame.
    "chart/line": Entry(
        '{% include "partials/_line_chart.html" %}',
        {"chart": _readings("Example pace", "Example readings over a week, two of them.", "Easy", "Hard")},
    ),
    "chart/line-empty": Entry(
        '{% include "partials/_line_chart.html" %}',
        {"chart": line_chart("Example pace", "Too few readings to draw.", (), unit="example")},
    ),
    "chart/stacked-bar": Entry(
        '{% include "partials/_stacked_bar_chart.html" %}',
        {"chart": _weeks("Example sessions", "Each week's sessions by kind.")},
    ),
    "chart/activity-calendar": Entry('{% include "partials/_activity_calendar.html" %}', {"calendar": _month()}),
    "chart/tabs": Entry(
        '{% include "partials/_chart_tabs.html" with group="example" %}',
        {"charts": (_readings("Example pace", "A week of pace.", "Pace"), _weeks("Example load", "Load by week."))},
    ),
    # Two charts in a pair, one line under each heading longer than the other's,
    # and a chart beside a section of prose: each pair's frames start together.
    "chart/pair": Entry(
        '<div class="two-col">{% include "partials/_line_chart.html" with chart=first %}'
        '{% include "partials/_stacked_bar_chart.html" with chart=second %}</div>',
        {
            "first": _readings(
                "Example pace",
                "An example line under the heading long enough to take two lines beside its neighbor.",
                "Pace",
            ),
            "second": _weeks("Example sessions", "Each week's sessions."),
        },
    ),
    "chart/beside-section": Entry(
        '<div class="two-col">{% include "partials/_line_chart.html" %}'
        '{% section "Example notes" %}<p>An example line of prose in the frame.</p>{% endsection %}</div>',
        {"chart": _readings("Example pace", "Example readings over a week.", "Pace")},
    ),
    # A row's buttons whose labels end in a name, one of them hyphenated, as a
    # queue row draws a plan for each container behind: the hyphenated part is
    # a box of its own (`breakable`), set on the line of the words before it.
    "part/row-buttons": Entry(
        '<div class="table-scroll"><table class="data-table"><tbody>{% for action in actions %}<tr><td>'
        '{% include "partials/_action_links.html" with actions=action only %}</td></tr>{% endfor %}</tbody></table></div>',
        {
            "actions": (
                (PageAction("Upgrade plan for example", "/example/"),),
                (PageAction("Upgrade plan for example-app", "/example/app/"),),
            )
        },
    ),
    # Facts ({% facts %}) in each layout and with each option; a new layout
    # or option of the tags comes with an entry here (test_facts_tags). A
    # zero beside a linked count stands on its value's line, as the Agents
    # card's counts do.
    "part/facts-band": Entry(
        '{% facts layout="band" label="Example status" %}'
        '{% fact "Example rules" value=0 %}'
        '{% fact "Example held for approval" value=2 url="/example/?held=1" tone="attention" %}'
        '{% fact "Example refused in the last day" value=13 url="/example/?refused=1" detail="An example detail" %}'
        "{% endfacts %}"
    ),
    "part/facts-band-icons": Entry(
        '{% facts layout="band" %}'
        '{% fact "Example running" icon="container" %}12{% detail %}<small>on 3 example machines</small>{% endfact %}'
        '{% fact "Example version" icon="layers" %}<code>example-1.2.3</code>{% detail %}'
        "<small>example.com/example/image</small><small>Tag only</small>{% endfact %}"
        '{% fact "An example label long enough to wrap in its cell" icon="shield" tone="serious" %}'
        '<a href="/example/">{{ "2\u00a0example findings"|breakable }}</a>{% detail %}<small>An example detail</small>{% endfact %}'
        '{% fact "Example checks" icon="check-circle" tone="good" value="Passing" %}'
        "{% endfacts %}"
    ),
    # An identifier longer than its fact's bound beside a plain value, then
    # one linked outside, as a container's Version and Update facts draw
    # them: each wraps a chip to a line and stands on its row's baseline.
    "part/facts-band-code": Entry(
        '{% facts layout="band" %}'
        '{% fact "Example running" icon="activity" value="Up 1 week" %}'
        '{% fact "Example version" icon="layers" %}<code>{{ tag|breakable }}</code>{% endfact %}'
        "{% endfacts %}"
        '{% facts layout="band" %}'
        '{% fact "Example running" icon="activity" value="Up 1 week" %}'
        '{% fact "Example update" icon="deploy" %}{% entity url|outside label=tag code=True %}{% endfact %}'
        "{% endfacts %}",
        {"tag": "2024.11.3-example2404-ls45-example-build", "url": "https://example.com/releases/2024.11.3"},
    ),
    # A detail given as lines, one of them holding a link among its words.
    "part/facts-band-lines": Entry(
        '{% facts layout="band" %}'
        '{% fact "Example certificate" value="example-com" url="/example/" detail=lines icon="lock" %}'
        '{% fact "Example health" icon="activity" tone="good" %}Passing{% detail %}'
        '<small>Forwards to example-app on <a href="/example/host/">example-host-01</a>.</small>'
        "<small>Read an example minute ago</small>{% endfact %}"
        "{% endfacts %}",
        {"lines": ("Example issuer", "24 example days left")},
    ),
    # A band beside a name in a card that lays out its parts in a flex row,
    # as a GitHub profile's counts sit beside its account: the band, whose
    # width is its frame's and never its facts', still holds them.
    "part/facts-band-in-a-row": Entry(
        '{% card %}<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 1rem 2rem">'
        "<p>An example account, and a line about it</p>"
        '{% facts layout="band" label="Example account" %}'
        '{% fact "Example followers" value=12 url="https://example.com/followers" %}'
        '{% fact "Example following" value=9 url="https://example.com/following" %}'
        '{% fact "Example repositories" value=4 url="https://example.com/repositories" %}'
        '{% fact "Example starred" value=38 url="https://example.com/stars" %}'
        "{% endfacts %}</div>{% endcard %}"
    ),
    # A card's summary line under its head: a zero, a linked count, a toned
    # count. Its labels read on from the value, so they are lower case.
    "part/facts-inline": Entry(
        '{% card %}<p>An example head of the card.</p>{% facts layout="inline" %}'
        '{% fact "example rules" value=0 %}'
        '{% fact "example held for approval" value=2 url="/example/?held=1" tone="attention" %}'
        '{% fact "example refused in the last day" value=13 url="/example/?refused=1" %}'
        "{% endfacts %}{% endcard %}"
    ),
    # Enough facts to wrap at the phone's width.
    "part/facts-inline-many": Entry(
        '{% card %}<p>An example head of the card.</p>{% facts layout="inline" %}'
        '{% fact "example rules" value=0 %}'
        '{% fact "example devices waiting" value=1 url="/example/?waiting=1" tone="serious" %}'
        '{% fact "example calls in the last hour" value=1204 url="/example/?calls=1" %}'
        '{% fact "example refused in the last day" value=13 url="/example/?refused=1" %}'
        '{% fact "example agents blocked" value=0 %}'
        "{% endfacts %}{% endcard %}"
    ),
    # A few short pairs, one of them a long address, which keep to their
    # content's width however wide the page is.
    "part/facts-list": Entry(
        '{% facts layout="list" %}'
        '{% fact "Address" %}<code>https://hq.example.com/mcp/</code>{% endfact %}'
        '{% fact "Sign-in" %}<a href="/example/">https://auth.example.com/application/o/example-hq-agent/'
        ".well-known/openid-configuration</a>{% endfact %}"
        '{% fact "Protocol" %}<code>2025-06-18</code>{% endfact %}'
        "{% endfacts %}"
    ),
    # Identifiers that wrap in a list held narrow, code, a bare path, an
    # entity's name and one linked outside, each under a fact of one line:
    # every value stands on its label's line, clear of the fact above it.
    "part/facts-list-identifiers": Entry(
        '<div style="max-inline-size: 300px">{% facts %}'
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example image" %}<code>{{ image|breakable }}</code>{% endfact %}'
        '{% fact "Example kind" value="Renewal" %}'
        '{% fact "Example path" value=path %}'
        '{% fact "Example port" value="8123" %}'
        '{% fact "Example serves" %}{% entity service %}{% endfact %}'
        '{% fact "Example state" value="Running" %}'
        '{% fact "Example update" %}{% entity url|outside label=tag code=True %}{% endfact %}'
        "{% endfacts %}</div>",
        {
            "image": "registry.example.com/example-team/example-image:2024.11.3-example-build",
            "path": "example-vault/renewals/example-renewal-with-a-long-name.md",
            "service": EntityLink(
                label="example-automation-dashboard.example.com", url="/example/", kind_label="Service"
            ),
            "tag": "2024.11.3-example2404-ls45-example-build",
            "url": "https://example.com/releases/2024.11.3",
        },
        premise=(_ONE_LINE, f".fact:nth-child(even) > {SELECTORS['fact_value']}"),
    ),
    "part/facts-list-many": Entry(
        "{% facts %}"
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example address" value="192.0.2.10" detail="Read an example minute ago" %}'
        '{% fact "Example owner" value=none %}'
        '{% fact "Example count" value=0 %}'
        '{% fact "Example link" value="example.com" url="/example/" %}'
        '{% fact "An example label long enough to take its column\'s room" value="Example value" %}'
        '{% fact "Example list" %}<ul class="dot-list"><li>example-one</li><li>example-two</li>'
        "<li>example-three</li></ul>{% endfact %}"
        '{% fact "Example prose" %}An example value that is a sentence long enough to wrap onto a second '
        "line at the narrower widths, as a reading's note does.{% endfact %}"
        "{% endfacts %}",
        {"none": None},
    ),
    # A run of links long enough to wrap, as a record's relationships list
    # what each phrase reaches: every line a target tall.
    "part/facts-list-links": Entry(
        "{% facts %}"
        '{% fact "Example forwards to" %}<ul class="dot-list">'
        '<li><a href="/example/one/">example-app-one</a></li><li><a href="/example/two/">example-app-two</a></li>'
        '<li><a href="/example/three/">example-app-three</a></li><li><a href="/example/four/">example-four</a></li>'
        '<li><a href="/example/five/">example-five</a></li><li><a href="/example/six/">example-app-six</a></li>'
        "</ul>{% endfact %}"
        "{% endfacts %}"
    ),
    # A list on the page and one in a disclosure under it, as a resource's
    # other settings are drawn: the list opened to read more sits closer.
    "part/facts-list-compact": Entry(
        '{% section "Example settings" %}{% facts %}'
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example image" %}<code>{{ image|breakable }}</code>{% endfact %}'
        "{% endfacts %}"
        '{% section "Example other settings" fold=True open=True %}{% facts %}'
        '{% fact "Example port" value="8123" %}'
        '{% fact "Example restart" value="unless-stopped" %}'
        '{% fact "Example network" %}<code>example-network</code>{% endfact %}'
        "{% endfacts %}{% endsection %}{% endsection %}",
        {"image": "registry.example.com/example-team/example-image:2024.11.3"},
        premise=(_TIGHTER, SELECTORS["facts_list"]),
    ),
    # The notice ({% notice %}) in each tone and with each option; a new tone
    # or option of the tag comes with an entry here (test_notice_tags).
    "part/notice-neutral": Entry(
        '{% notice css="example" data_example="example" %}An example line that needs saying first, '
        'with <a href="/example/">an example link</a>.{% endnotice %}'
    ),
    "part/notice-titled": Entry(
        '{% notice tone="warning" title="2 example jobs failed on their last run" %}'
        "<p>example-backup, example-sync</p>{% endnotice %}"
    ),
    # A title the page composes, written as the tag's title part.
    "part/notice-composed": Entry(
        '{% notice tone="warning" %}<p>Pick another below.</p>'
        "{% title %}{{ name|breakable }} cannot be used here{% endnotice %}",
        {"name": "example-target.example.com"},
    ),
    "part/notice-info": Entry('{% notice tone="info" %}An example line of news.{% endnotice %}'),
    "part/notice-success": Entry('{% notice tone="success" %}Example saved.{% endnotice %}'),
    "part/notice-error": Entry(
        '{% notice tone="error" %}An example failure long enough to take more than one line on a phone, '
        "so its wrapping is read too.{% endnotice %}"
    ),
    # Among the page's messages, as a post's outcome is said: each a toast.
    "part/notice-messages": Entry(
        '{% include "partials/_messages.html" %}',
        {
            "messages": [
                Message(level, f"An example {name} message.")
                for level, name in ((SUCCESS, "success"), (INFO, "info"), (WARNING, "warning"), (ERROR, "error"))
            ]
        },
    ),
    # An error page, which titles itself outside the page frame: one with the
    # search field, as a missing page offers it, and one naming the request
    # id its log line carries, long enough to break at its dots.
    "part/error-page": Entry(
        '{% include "partials/_error_page.html" with code="404" title="Page not found" '
        'body="The example link may be old, or what it pointed to was deleted." search=True %}',
        heads_the_page=True,
    ),
    "part/error-page-request": Entry(
        '{% include "partials/_error_page.html" with code="500" title="Something broke" '
        'body="The example error is in the log. Trying again is safe." request_id=request_id %}',
        {"request_id": "example-0123abcd.4567ef89.0123abcd4567"},
        heads_the_page=True,
    ),
    # A card that is the whole of a page outside the frame, as the sign-in
    # draws one: its mark, its own title, its one action and its closing line.
    "part/card-alone": Entry(
        '{% load static %}{% card element="div" alone=True label="Example sign-in" %}'
        '<img src="{% static "img/logo.png" %}" alt="Example" width="64" height="64">'
        "<h1>Example sign-in</h1>"
        '<p class="muted">An example line before the action.</p>'
        '<p><a class="btn primary" href="/example/">Use example sign-on</a></p>'
        '<p><small class="muted">An example closing line.</small></p>{% endcard %}',
        heads_the_page=True,
    ),
    # An account named as whose it is, in a card above its band of counts, as
    # the Starred page draws your GitHub profile: its picture, its name with
    # a state beside it, its handle and a line in its own words.
    "part/identity": Entry(
        '{% card label="Example profile" %}{% include "partials/_identity.html" with picture=picture '
        'name="Example Person" handle="@example" url="https://example.com/example" badge="Your account" '
        'tone="good" tip="An example reason it is yours." about="Runs an example homelab." only %}'
        '{% facts layout="band" %}{% fact "Example followers" value=12 %}{% fact "Example repos" value=4 %}'
        "{% endfacts %}{% endcard %}",
        {
            "picture": (
                "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8'%3E"
                "%3Crect width='8' height='8' fill='%23888'/%3E%3C/svg%3E"
            )
        },
    ),
    # A saved copy's About dialog body: what to try, its own pages as chips,
    # and who made it.
    "part/demo-about": Entry('{% include "partials/_demo_about.html" %}', {"saved_copy": _NOW}),
    # A saved copy's footer, which says from the page's corner what it is.
    "part/demo-foot": Entry('{% include "partials/_demo_foot.html" %}', frame="after"),
    # Command Center's palette as its dialog opens on it, waiting for its
    # results; its results for a term, each kind of group with more than it
    # shows; and a term nothing answers.
    "part/command-center": Entry('{% include "partials/_command_center.html" %}'),
    "part/command-center-results": Entry('{% include "partials/_command_center.html#results" %}', _palette()),
    "part/command-center-empty": Entry('{% include "partials/_command_center.html#results" %}', {"q": "example-none"}),
    # The form page's card (form_page.html) with fields and what saving does,
    # one with no fields, which is its closing row alone, and a form that asks
    # (partials/_form.html method="get"), one question on its button's line,
    # once more with its help and an error under them; a new option of either
    # comes with an entry here (test_form_page).
    "form/page": Entry(
        '{% include "form_page.html#form_card" %}',
        {
            "form": _ExampleForm(),
            "form_verb": "Add example",
            "cancel_url": "#",
            "form_note": "An example note on what saving does, long enough to wrap on a phone.",
        },
    ),
    "form/page-fieldless": Entry(
        '{% include "form_page.html#form_card" %}', {"form": _NoFields(), "form_verb": "Run example", "cancel_url": "#"}
    ),
    "form/page-pairs": Entry(
        '{% include "form_page.html#form_card" %}',
        {"form": _ExampleRange(), "form_verb": "Add example", "cancel_url": "#"},
    ),
    # A form whose connection cannot make the change (`gap`): its verb drawn
    # unusable, with what it needs beside it.
    "form/write-gap": Entry(
        '{% section "Example record" %}{% include "partials/_form.html" with form=form verb="Save example" '
        'cancel_url="#" gap=gap note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {
            "form": _ExampleForm(),
            "gap": WriteGap(
                "HQ only reads through Example connection example-main. A change through it also needs Example Write.",
                ONLY_READS,
                "Needs manages: 1 and Example Write",
                "#",
            ),
        },
    ),
    # A form whose answers each say what choosing them does (`ChoiceCards`), one
    # card a choice, and a number with its unit after the box
    # (`SuffixedInput`), sent with the number past its limit.
    "form/choice-cards": Entry(
        '{% section "Example policy" %}{% include "partials/_form.html" with form=form verb="Save example" '
        'cancel_url="" note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {"form": _ExampleChoices(data={"answer": "refuse", "share": "150"})},
    ),
    "form/lookup": Entry(
        '{% section "Example lookup" %}{% include "partials/_form.html" with method="get" form=form verb="Look up" '
        'cancel_url="" note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {"form": _ExampleLookup()},
        premise=(_ONE_QUESTION, _QUESTION_PARTS),
    ),
    "form/lookup-help": Entry(
        '{% section "Example group" %}{% include "partials/_form.html" with form=form verb="Add example group" '
        'cancel_url="" note="" choices="" choice_field="" bar="" %}{% endsection %}',
        {"form": _ExampleNaming(data={})},
        premise=(_ONE_QUESTION, _QUESTION_PARTS),
    ),
    # The filter bar ({% filter_bar %}) narrowed, with its way back and its
    # count; with one choice and nothing narrowed; and with labeled choices
    # asking another page, its verb its own. A new option of the tag comes
    # with an entry here (test_filter_tags).
    "form/filter-bar": Entry(
        '{% filter_bar filtered=True clear="#" %}'
        '<input type="search" name="q" value="example" placeholder="Filter example records" '
        'aria-label="Filter example records">'
        '<select name="state" aria-label="Example state"><option value="">Any example state</option>'
        '<option value="on" selected>Online</option></select>'
        '<select name="kind" aria-label="Example kind"><option value="">All example kinds</option></select>'
        "{% summary %}<strong>3 of 12 example records</strong>, 1 needs a look{% endfilter_bar %}"
    ),
    "form/filter-bar-one": Entry(
        '{% filter_bar %}<select name="year" aria-label="Example year"><option>2026</option>'
        "<option>2025</option></select>{% endfilter_bar %}"
    ),
    "form/filter-bar-labeled": Entry(
        '{% filter_bar verb="Check" action="#" css="example" data_example="example" %}'
        '{% control_label "From" control="example-from" %}'
        '<select id="example-from" name="from"><option>example-host-01</option></select>'
        '{% control_label "To" control="example-to" %}'
        '<select id="example-to" name="to"><option>example-host-02</option></select>'
        "{% endfilter_bar %}"
    ),
    # A control label ({% control_label %}) with no control, naming the answer
    # that follows it; the filter bar above draws it for its controls
    # (test_label_tags).
    "part/control-label": Entry(
        '{% card element="article" label="Example stage" %}{% control_label "Example receiving" %}'
        "<p><strong>Example provider</strong></p>{% endcard %}"
    ),
    # A "do this now" control in a section's body: one never pressed, beside
    # one whose work ended and says how.
    "part/ask": Entry(
        '{% section "Example readings" %}<p>{% include "partials/_ask.html" with ask=idle %}</p>'
        '<p>{% include "partials/_ask.html" with ask=done %}</p>{% endsection %}',
        {
            "idle": Ask("Read example-host-01", "/example/read/", title="Reads example-host-01 again."),
            "done": Ask(
                "Read example-host-01",
                "/example/read/",
                standing=Standing(DONE, note="Read 12 example records.", since=_NOW - timedelta(minutes=3)),
            ),
        },
    ),
    # While its work is under way: busy, its elapsed time ticking, followed
    # at its status resource.
    "part/ask-running": Entry(
        '{% section "Example readings" %}{% include "partials/_ask.html" with ask=ask %}{% endsection %}',
        {
            "ask": Ask(
                "Read example-host-01",
                "/example/read/",
                standing=Standing(
                    RUNNING,
                    note="Reading every example record example-host-01 holds, which takes a minute.",
                    since=_NOW - timedelta(seconds=40),
                    percent=40,
                ),
                status_url="/example/read/status/",
                primary=True,
            )
        },
    ),
    # In a row's cell, where its word alone stands and its sentence opens
    # under the row: one that failed with a long sentence.
    "part/ask-in-a-row": Entry(
        '{% section "Example connections" %}{% table_region "Example connections" %}<table class="data-table">'
        '<thead><tr>{% table_heading "Name" "identifier" %}{% table_heading "Read" "control" %}</tr></thead>'
        '<tbody><tr><td>example-main</td><td>{% include "partials/_ask.html" with ask=ask %}</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "ask": Ask(
                "Read now",
                "/example/read/",
                standing=Standing(
                    FAILED,
                    note="The example provider refused the credential, so nothing was read this time.",
                    since=_NOW - timedelta(minutes=8),
                ),
                compact=True,
            )
        },
    ),
    # As a page's own action, beside a link, in its head.
    "part/ask-in-the-head": Entry(
        '{% include "partials/_page_head.html" %}',
        {
            "page": Page(
                title="Example host",
                actions=(
                    Ask("Read now", "/example/read/", title="Reads example-host-01 again."),
                    PageAction("Edit", "/example/edit/"),
                ),
            )
        },
        heads_the_page=True,
    ),
    # A job under way, as far along as it knows; one waiting its turn, which
    # knows nothing yet; one that failed with a long note.
    "part/job-progress": Entry(
        '{% include "partials/_job_progress.html" %}',
        {
            "job": Job(
                id=UUID(int=1),
                kind="example.import",
                label="Example import",
                state=JobState.RUNNING,
                percent=40,
                note="Read 40 of 100 example rows.",
                created_at=_NOW,
                heartbeat_at=_NOW,
            )
        },
    ),
    "part/job-progress-waiting": Entry(
        '{% include "partials/_job_progress.html" %}',
        {"job": Job(id=UUID(int=2), kind="example.import", label="Example import", created_at=_NOW)},
    ),
    "part/job-progress-failed": Entry(
        '{% include "partials/_job_progress.html" %}',
        {
            "job": Job(
                id=UUID(int=3),
                kind="example.import",
                label="Example import of a file with a name long enough to wrap",
                state=JobState.FAILED,
                note="The example file has no header row, so none of its rows could be read as example records.",
                created_at=_NOW,
            )
        },
    ),
    # A row of places to go, each a chip: one of HQ's own pages, outside ones
    # with their arrow, and a name long enough to wrap on a phone.
    "part/link-chips": Entry(
        '{% section "Example links" %}{% link_chips %}'
        '<li><a href="/example/" title="An example page of HQ">Example page</a></li>'
        '<li>{% entity "https://example.com/"|outside label="Example site" %}</li>'
        '<li>{% entity "https://docs.example.com/"|outside label="Example documentation" %}</li>'
        '<li>{% entity "https://example.org/"|outside label="An example tool whose name runs long enough to wrap" %}</li>'
        "{% endlink_chips %}{% endsection %}"
    ),
    # Whether a thing happened, week by week: hits, a miss, a week counted
    # more than once, one nothing is known of, and a run up to now.
    "chart/cadence": Entry(
        '{% section "Example habits" %}{% include "partials/_cadence.html" %}{% endsection %}',
        {
            "cadence": Cadence(
                "Example stretching",
                "Each week it happened at least once.",
                (
                    CadenceWeek("Jan 5", hit=True, detail="Week of Jan 5: once"),
                    CadenceWeek("Jan 12", hit=False, detail="Week of Jan 12: not done"),
                    CadenceWeek("Jan 19", hit=False, no_data=True),
                    CadenceWeek("Jan 26", hit=True, count=3, detail="Week of Jan 26: 3 times"),
                    CadenceWeek("Feb 2", hit=True, detail="Week of Feb 2: once"),
                    CadenceWeek("Feb 9", hit=True, count=2, detail="Week of Feb 9: twice"),
                ),
            )
        },
    ),
    # Several things over the same weeks, one a link with a line about it.
    "chart/cadence-matrix": Entry(
        '{% section "Example habits" %}{% include "partials/_cadence_matrix.html" %}{% endsection %}',
        {"matrix": _matrix()},
    ),
    # The same, sharing a row with a chart.
    "chart/cadence-matrix-compact": Entry(
        '<div class="two-col">{% section "Example habits" %}'
        '{% include "partials/_cadence_matrix.html" with compact=True label="Example habit" %}{% endsection %}'
        '{% include "partials/_line_chart.html" %}</div>',
        {"matrix": _matrix(), "chart": _readings("Example minutes", "Minutes each day.", "Example run")},
    ),
    # The days of a week a thing is planned for: two on one day, a day with
    # its note, the notes under the week and the link that changes it.
    "chart/week-plan": Entry(
        '{% include "partials/_week_plan.html" %}',
        {
            "plan": WeekPlan(
                "Example week",
                (
                    PlannedDay("M", "Monday", (1,), detail="Example run"),
                    PlannedDay("T", "Tuesday"),
                    PlannedDay("W", "Wednesday", (1, 2), note="Long", detail="Example run and example lift"),
                    PlannedDay("T", "Thursday"),
                    PlannedDay("F", "Friday", (2,), detail="Example lift"),
                    PlannedDay("S", "Saturday", (1,), note="Easy", detail="Example run"),
                    PlannedDay("S", "Sunday"),
                ),
                notes=(PlanNote("Example runs", "3 a week"), PlanNote("Example lifts", "2 a week")),
                url="/example/plan/",
            )
        },
    ),
    # Readings that need reading, each tone: one a link, one with its next
    # step, one with neither a value nor an eyebrow.
    "part/insight-grid": Entry(
        '{% section "Example insights" %}{% include "partials/_insight_grid.html" with label="Example insights" %}'
        "{% endsection %}",
        {
            "items": [
                Insight(
                    "attention",
                    "Example readiness",
                    "Example records missing a category",
                    "12",
                    "Uncategorized example records are left out of every example total.",
                    action="Give each one a category.",
                    url="/example/records/",
                ),
                Insight("good", "Example pace", "Example pace this month", "On track", "Ahead of the example plan."),
                Insight(
                    "serious",
                    "Example spend",
                    "An example reading with a title long enough to wrap on a phone",
                    "$1,204.50",
                    "Spent past the example budget by a margin worth reading about.",
                ),
                Insight("neutral", "", "An example reading with nothing to show", "", "An example interpretation."),
            ]
        },
    ),
    # A file field that takes a drop: as it is first drawn, and sent with
    # nothing in it.
    "form/dropzone": Entry(
        '{% section "Example import" %}{% include "partials/_dropzone.html" with field=form.exports '
        'title="Drop example exports here" hint="CSV or JSON, several at once" %}{% endsection %}',
        {"form": _ExampleFiles()},
    ),
    "form/dropzone-errors": Entry(
        '{% section "Example import" %}{% include "partials/_dropzone.html" with field=form.exports %}{% endsection %}',
        {"form": _ExampleFiles(data={})},
    ),
    # A command to paste somewhere else, short and long enough to wrap at its
    # slashes and spaces.
    "part/command": Entry(
        '{% section "Example setup" %}{% include "partials/_command.html" with command="example-cli status" %}'
        '{% include "partials/_command.html" with command=long %}{% endsection %}',
        {
            "long": "example-cli connect --config /etc/example/example-agent/config.toml "
            "--token-file /run/secrets/example-agent-token --name example-host-01.example.com"
        },
    ),
    # Which network the link rides over, as the panel's Connection row says
    # it: across the internet with the address to look up, through a relay,
    # and on the reader's own network.
    "part/connection-carriage": Entry(
        '{% section "Example link" %}{% facts %}'
        '{% fact "Connection" %}{% include "partials/_connection_peering.html#carriage" with peering=internet only %}'
        "{% endfact %}"
        '{% fact "Relayed" %}{% include "partials/_connection_peering.html#carriage" with peering=relay only %}'
        "{% endfact %}"
        '{% fact "Local" %}{% include "partials/_connection_peering.html#carriage" with peering=local only %}'
        "{% endfact %}{% endfacts %}{% endsection %}",
        {
            "internet": Peering("internet", "Direct, over the internet", address="203.0.113.7"),
            "relay": Peering("relay", "Relayed via DERP example-region"),
            "local": Peering("local", "Direct, on your network", address="192.0.2.30"),
        },
    ),
    # The connection dialog before its panel arrives.
    "part/connection-loading": Entry('{% include "core/_connection_loading.html" %}'),
    # You, the link and HQ: over the tailnet with every check confirmed and
    # both ends leading to their machines, under Advanced HQ Posture.
    "part/connection-peering": Entry(
        '{% include "partials/_connection_peering.html" %}',
        {"connection": _connection(), "posture": _posture((True, True))},
    ),
    # Off the tailnet with no reading of it: the address alone, and HQ by
    # the name it was asked for.
    "part/connection-peering-network": Entry(
        '{% include "partials/_connection_peering.html" %}',
        {"connection": _connection(NETWORK_CHANNEL, path="", linked=False)},
    ),
    # Relayed, a check not yet read, neither end a known machine, and a
    # posture short of one check with another unread.
    "part/connection-peering-relayed": Entry(
        '{% include "partials/_connection_peering.html" %}',
        {
            "connection": _connection(path="relayed", held=False, linked=False),
            "posture": _posture((True, False, None), advanced=False),
        },
    ),
    # Dots beside a figure, a part of a whole and a history whose dots lead
    # to each run; and a history in a table's cell.
    "part/figure-dots": Entry(
        '{% section "Example hosts" %}{% include "partials/_kpi_grid.html" %}{% endsection %}',
        {
            "items": [
                Kpi("Example hosts online", "3 of 5", "2 example hosts offline", drawing=Dots.of(3, 5)),
                Kpi("Example runs", "4 of 6", "The last six example runs", drawing=_runs()),
            ]
        },
    ),
    "part/dots-in-a-row": Entry(
        '{% section "Example work" %}{% table_region "Example work" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" %}{% table_heading "Runs" %}</tr></thead><tbody>'
        '<tr><td>example.sweep</td><td>{% include "partials/_dots.html" with dots=runs only %}</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}",
        {"runs": _runs()},
    ),
    # On a saved copy of HQ, what a page says of the reader's connection is
    # said of the example visit: as a panel's notice, and as the short mark.
    "part/example-connection": Entry(
        '{% section "Example connection" %}{% include "partials/_example_connection.html" with notice=True %}'
        '{% facts %}{% fact "Example address" %}192.0.2.10 '
        '{% include "partials/_example_connection.html" %}{% endfact %}{% endfacts %}{% endsection %}',
        settings={"SEVERINO_RENDERED_COPY": True},
    ),
    # A list's pages, at a middle page, and on the first, which has none before it.
    "part/pagination": Entry(
        '{% section "Example events" %}{% table_region "Example events" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Event" %}</tr></thead><tbody><tr><td>Example event</td></tr></tbody></table>'
        '{% endtable_region %}{% include "partials/_pagination.html" %}{% endsection %}',
        _paged(2),
        address="/example/events/?q=example&page=2",
    ),
    "part/pagination-first": Entry(
        '{% section "Example events" %}{% include "partials/_pagination.html" %}{% endsection %}',
        _paged(1),
        address="/example/events/",
    ),
    # A list's toolbar over its table: a search with words in it, a filter
    # with some of its options chosen, a toggle and a sort.
    "part/table-toolbar": Entry(
        '{% include "partials/_table_toolbar.html" %}{% table_region "Example hosts" %}<table class="data-table">'
        '<thead><tr>{% table_heading "Name" "identifier" %}{% table_heading "State" "status" %}</tr></thead>'
        "<tbody><tr><td>example-01</td><td>Online</td></tr><tr><td>example-02</td><td>Offline</td></tr>"
        "</tbody></table>{% endtable_region %}",
        {"table": _toolbar()},
        address="/example/hosts/?q=example&kind=machine&stale=1",
    ),
    # A row's editor: the way in, closed, and the address that opens the
    # first row's form in the line beneath it.
    "part/row-editor": Entry(_EDITED_ROWS, {"form": _ExampleDistance()}, address="/example/runs/"),
    "part/row-editor-open": Entry(_EDITED_ROWS, {"form": _ExampleDistance()}, address="/example/runs/?edit=example-01"),
    # The open problems about the thing a page shows: an urgent one in its
    # box above, and the rest in a card of their own.
    "part/problems": Entry(
        '{% include "partials/_problems.html" %}',
        {
            "problems": [
                *_problems("example-host-01 is out of disk", status="serious"),
                *_problems("example-web-01 runs an image by tag alone", "example-worker-02 restarts often"),
            ]
        },
    ),
    # A reading's age in each of its states: current, out of date, the moment
    # alone, and none.
    "part/reading-age": Entry(
        '{% section "Example readings" %}{% facts %}'
        '{% fact "Example current" %}{% include "partials/_reading_age.html" with reading=current only %}{% endfact %}'
        '{% fact "Example stale" %}{% include "partials/_reading_age.html" with reading=stale only %}{% endfact %}'
        '{% fact "Example moment" %}{% include "partials/_reading_age.html" with at=at only %}{% endfact %}'
        '{% fact "Example never" %}{% include "partials/_reading_age.html" %}{% endfact %}'
        "{% endfacts %}{% endsection %}",
        {
            "current": Freshness(CURRENT, _NOW - timedelta(minutes=4), timedelta(hours=1)),
            "stale": Freshness(STALE, _NOW - timedelta(days=3), timedelta(hours=1)),
            "at": _NOW - timedelta(hours=2),
        },
    ),
    # A row of readings beside a page's title, as the dashboard draws it: one
    # that leads to a dialog with a note of a problem, one whose line needs
    # attention, and a region holding readings that open a panel, one
    # needing attention and one past its time.
    "part/readings": Entry(
        '{% include "partials/_page_head.html" %}{% section "Example section" %}<p>An example line.</p>{% endsection %}',
        {"page": Page(title="Example readings"), "page_aside": _Drawn(_READINGS)},
        heads_the_page=True,
    ),
    # The same row on its own line, as a narrower head draws it: a reading
    # with nothing read yet, a long line that wraps inside its reading, and
    # one of its own, outside any region.
    "part/readings-under-the-title": Entry(
        '{% include "partials/_page_head.html" %}{% section "Example section" %}<p>An example line.</p>{% endsection %}',
        {
            "page": Page(title="Example readings", lede="An example lede long enough to take the head's line."),
            "page_facts": "An example fact · another example fact",
            "page_aside": _Drawn(_READINGS_EMPTY),
        },
        heads_the_page=True,
    ),
    # The facts on a title's line ({% title_meta %}): a run of parts with a
    # dot between each, long enough to wrap under the title on a phone; and
    # as a list whose parts each wrap whole. A new option of the tag comes
    # with an entry here (test_title_tags).
    "part/title-meta": Entry(
        '{% include "partials/_page_head.html" %}{% section "Example section" %}<p>An example line.</p>{% endsection %}',
        {
            "page": Page(title="example-host-01"),
            "page_aside": _Drawn(
                '{% title_meta %}{% status "Online" tone="good" %} since 3 days ago{% dot %}example role'
                "{% dot %}<code>192.0.2.10</code>{% dot %}also known as <code>example-host-01.example.com</code>"
                "{% endtitle_meta %}"
            ),
        },
        heads_the_page=True,
    ),
    "part/title-meta-list": Entry(
        '{% include "partials/_page_head.html" %}{% section "Example section" %}<p>An example line.</p>{% endsection %}',
        {
            "page": Page(title="Example record"),
            "page_aside": _Drawn(
                '{% title_meta element="ul" data_example="example" %}'
                '<li><strong>{% status "Out of date" tone="attention" pill=False %}</strong> since 2 hours ago</li>'
                '<li>{% status "An example sentence that says what the state means." tone="attention" pill=False %}</li>'
                "{% endtitle_meta %}"
            ),
        },
        heads_the_page=True,
    ),
    # How old each provider's last reading is: folded as a grid, one of them
    # out of date and one unreachable; one provider's as a sentence; and its
    # age alone, in a heading's aside.
    "part/sweep-freshness": Entry('{% include "partials/_sweep_freshness.html" %}', {"inventory": _INVENTORY}),
    "part/sweep-freshness-one": Entry(
        '{% section "Example records" %}{% include "partials/_sweep_freshness.html" with only_kind="example.records" %}'
        "{% endsection %}",
        {"inventory": _INVENTORY},
    ),
    "part/sweep-freshness-inline": Entry(
        '{% section "Example records" %}<p>An example line.</p>{% aside %}<span class="count">12 example records'
        '{% include "partials/_sweep_freshness.html" with only_kind="example.records" inline=True %}</span>'
        "{% endsection %}",
        {"inventory": _INVENTORY},
    ),
    # A switch ({% switch %}): siblings with one current, a mark and a note on
    # some, and the summary of the current one under the row.
    "part/switch": Entry(
        '{% switch label="Example domains" %}'
        '{% choice url="/example/one/" current=True mark="★" %}example.com{% endchoice %}'
        '{% choice url="/example/two/" %}example.net{% endchoice %}'
        '{% choice url="/example/three/" note="not managed" %}example-long-domain-name.example.org{% endchoice %}'
        "{% summary %}An example line that says what the current choice answers.{% endswitch %}"
    ),
    # The same switch with a count beside each chip, nought included.
    "part/switch-counted": Entry(
        '{% switch label="Example status" %}'
        '{% choice url="?" current=True count=128 %}All{% endchoice %}'
        '{% choice url="?status=example-new" count=12 %}Example new{% endchoice %}'
        '{% choice url="?status=example-read" count=0 %}Example read{% endchoice %}'
        "{% endswitch %}"
    ),
    # A page that asks for its readings when opened: the note that ends its
    # head's first line, and a region that fills itself holding its own.
    "part/visit-refresh": Entry(
        '{% include "partials/_page_head.html" %}{% include "partials/_visit_refresh.html" with visit=page_visit %}'
        '{% section "Example host" %}<p>An example line.</p>{% endsection %}',
        {
            "page": Page(title="Example host", trail=(("Example machines", "/example/"),)),
            "page_visit": PageVisit("machine", "example-host-01"),
        },
        heads_the_page=True,
    ),
    "part/visit-refresh-region": Entry(
        '{% card element="div" data_example_region=True %}{% include "partials/_visit_refresh.html" with '
        'subject="page" name="example" refresh="[data-example-region]" %}<p>An example line.</p>{% endcard %}'
    ),
    # A horizon of dated events, more than one column holds on a wide
    # screen: a link, one with a line under it and a badge, each tone.
    "part/timeline": Entry(
        '{% include "partials/_timeline.html" %}',
        {
            "timeline": Timeline(
                "Example horizon",
                "What is coming up for the example records.",
                (
                    TimelineItem(date(2026, 1, 5), "Example renewal", url="/example/renewal/"),
                    TimelineItem(
                        date(2026, 1, 19),
                        "Example certificate with a name long enough to wrap",
                        detail="example-host-01.example.com",
                        status="attention",
                        badge="Due soon",
                    ),
                    TimelineItem(date(2026, 2, 2), "Example review", detail="Every quarter", status="good"),
                    TimelineItem(date(2026, 3, 16), "Example payment", status="serious", badge="Overdue"),
                ),
            )
        },
    ),
    # What a record is related to: a group whose ends each say who read them
    # and when, one a single source read, and one of names alone.
    "part/relationships": Entry(
        '{% include "partials/_relationships.html" %}',
        {
            "relationships": Relationships(
                "example-host-01",
                groups=(
                    RelationGroup(
                        "Reached through",
                        1,
                        (
                            Relationship(
                                EntityLink("example-proxy-01", "/example/proxy/", kind="machine"),
                                source=EntityLink("Example provider", "/example/provider/"),
                                observed_at=_NOW - timedelta(hours=3),
                            ),
                            Relationship(
                                EntityLink("example-proxy-02", "/example/proxy-2/", detail="an example record"),
                                source=EntityLink("Another example provider", "/example/provider-2/"),
                                observed_at=_NOW - timedelta(days=2),
                                stale=True,
                            ),
                        ),
                    ),
                    RelationGroup(
                        "Runs",
                        2,
                        tuple(
                            Relationship(
                                EntityLink(f"example-service-{number}", f"/example/service-{number}/"),
                                source=EntityLink("Example provider", "/example/provider/"),
                                observed_at=_NOW - timedelta(minutes=20),
                            )
                            for number in range(1, 6)
                        ),
                    ),
                    RelationGroup(
                        "Names",
                        3,
                        (Relationship(EntityLink("example.com")), Relationship(EntityLink("www.example.com"))),
                    ),
                ),
                focus_url="/example/topology/",
            )
        },
    ),
    # A request's route as its strip of hops, every state a hop takes: proven
    # by the request (so the key under the strip says so), contradicted, not
    # read, with a certificate, with facts under its name, and a name long
    # enough to break.
    "part/path-hops": Entry(
        '{% section "Example path" %}{% include "control_plane/_path_hops.html" with route=route '
        'label="Path via example record" %}{% endsection %}',
        {"route": _route()},
    ),
    # The services table's name cell: favorites with the pair that moves them
    # (the first with no way up, the last with no way down), and a service
    # that is not one, which draws its star alone.
    "part/service-pin": Entry(
        '{% section "Example services" %}{% table_region "Example services" %}<table class="data-table"><thead>'
        '<tr>{% table_heading "Service" "identifier" gutter=True %}{% table_heading "Open to" %}</tr></thead>'
        "<tbody>{% for service in favorites %}<tr><td>"
        '{% include "control_plane/_service_pin.html" with service=service reorderable=True '
        "first=forloop.first last=forloop.last only %}</td><td>Example network</td></tr>{% endfor %}"
        "{% for service in others %}<tr><td>"
        '{% include "control_plane/_service_pin.html" with service=service only %}</td><td>Example network</td></tr>{% endfor %}'
        "</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "favorites": [
                _Service("app.example.com", pinned=True),
                _Service("example-dashboard-with-a-long-name.example.com", pinned=True),
                _Service("home.example.com", pinned=True),
            ],
            "others": [_Service("shop.example.com")],
        },
    ),
    # A dense page's map, more links than a phone's row holds, over the
    # sections it opens.
    "part/page-navigation": Entry(
        '{% include "partials/_page_head.html" %}{% include "partials/_page_navigation.html" with '
        "navigation=page.navigation %}{% for item in page.navigation.items %}"
        "{% section item.label id=item.id %}<p>An example line.</p>{% endsection %}{% endfor %}",
        {
            "page": Page(
                title="Example host",
                navigation=PageNavigation(
                    tuple(
                        PageSection(f"example-{number}", label)
                        for number, label in enumerate(
                            (
                                "Overview",
                                "Example readings",
                                "Example containers",
                                "Example certificates",
                                "Relationships",
                                "Example history",
                            )
                        )
                    )
                ),
            )
        },
        heads_the_page=True,
    ),
    # A value and the one that replaces it: as words, and as a cell's own
    # identifiers, one pair linking outside HQ and one too long to share a
    # line with its old value beside a note that asks for its whole length.
    "part/change": Entry(
        '{% section "Example versions" %}<p>{% include "partials/_change.html" with from="1.2" to="1.4" %}</p>'
        '{% table_region "Example images" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" %}{% table_heading "Image" %}{% table_heading "Note" %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% include "partials/_change.html" with from="example:1.2" '
        'to="example:1.4" url="https://example.com/releases/" code=True %}</td><td>An example release</td></tr>'
        '<tr><td>example-02</td><td>{% include "partials/_change.html" with from=old to=new code=True %}'
        "</td><td>Restarted once the example update was pulled from the example registry</td></tr></tbody>"
        "</table>{% endtable_region %}{% endsection %}",
        {"old": "example:2026.1.0", "new": "example:2026.3.0"},
    ),
}


def part_page(name, markup, before="", after=""):
    """A page titled ``name``, of HQ's stylesheets, with ``markup`` alone in
    its main, and the frame's parts base.html draws before and after it."""

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(name)}</title>{stylesheets()}</head>"
        f'<body>{before}<main class="content" id="content">{markup}</main>{after}</body></html>'
    )


class CatalogLayoutTests(LayoutBrowserTests):
    """Every catalog entry under the whole survey, without script, at the
    sparse widths, which cross the stylesheet's media queries: closed, in both
    palettes, and with every disclosure open."""

    @classmethod
    @override
    def render(cls):
        return {name: entry.page(name) for name, entry in CATALOG.items()}

    def test_every_entry_keeps_the_layout_rules_without_script(self):
        for name in CATALOG:
            for width in SPARSE_WIDTHS:
                if not self.loaded(name, width):
                    continue
                self.hold(name, width, [self.draws_the_state_it_stands_for, self.every_layout_check_holds])
                self.in_scheme("dark")
                self.hold(name, width, [self.every_palette_check_holds], scheme="dark")
                self.in_scheme("light")
                self.hold(name, width, [self.open_every_disclosure, self.every_layout_check_holds], disclosures="open")

    def test_every_drawing_is_drawn_on_a_phone(self):
        """A chart, a trend, a bar or a calendar is drawn wherever what holds
        it is: on a phone a drawing hidden for want of room is a figure that
        shows nothing, when the table beside it or the line under it has the
        room to show it."""

        for name in CATALOG:
            if not self.loaded(name, PHONE_SCREEN[0]):
                continue
            with self.subTest(page=name):
                self.assertEqual(self.read([(_UNDRAWN, SELECTORS["drawing"])])[0], [])

    def draws_the_state_it_stands_for(self, name):
        if premise := CATALOG[name].premise:
            self.assertEqual(self.read([premise])[0], [], f"{name} does not draw the state it stands for")
