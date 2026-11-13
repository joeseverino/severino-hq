"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. A new state of a shared part is
one entry here."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, override
from unittest import mock

from django.template import Context, Template
from django.template.loader import render_to_string
from django.utils.html import escape

from hq.platform.application import calendar
from hq.platform.application.calendar import CalendarEvent, CalendarSource, calendar_month
from hq.platform.application.drawings import AllocationBar, Share, Trend
from hq.platform.application.pages import Page, PageAction
from hq.platform.application.ui import (
    ActivityCalendar,
    CalendarDay,
    ChartSeries,
    Kpi,
    line_chart,
    stacked_bar_chart,
)
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, SPARSE_WIDTHS
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets


@dataclass(frozen=True)
class Entry:
    """A part as a template writes it, with what it is given.

    ``premise`` is what the entry must draw to stand for its state, as a probe
    and its argument returning what falls short of it: a value meant to wrap
    that fits on one line holds nothing the survey could find wrong.
    ``heads_the_page`` says the part is a page's own head; any other is drawn
    under one, as every HQ page is."""

    template: str
    context: dict[str, Any] = field(default_factory=dict)
    premise: tuple[str, Any] | None = None
    heads_the_page: bool = False

    def drawn(self, name):
        head = "" if self.heads_the_page else render_to_string(HEAD, {"page": Page(title=name)})
        return head + Template(self.template).render(Context(self.context))


HEAD = "partials/_page_head.html"

# Each value that fits on one line of its own.
_ONE_LINE = """(values) => [...document.querySelectorAll(values)]
  .filter((value) => Math.round(value.getBoundingClientRect().height / parseFloat(getComputedStyle(value).lineHeight)) < 2)
  .map((value) => value.textContent.trim())"""


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


CATALOG = {
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
    # A list whose rows carry their own padding fills the frame as a table does.
    "part/section-rows": Entry(
        '{% section "Example repositories" count=2 %}<ul class="github-list">'
        '<li><a href="/example/one/">example-one</a> <span class="muted">Example access, read only</span></li>'
        '<li><a href="/example/two/">example-two</a> <span class="muted">Example access, read only</span></li>'
        "</ul>{% endsection %}"
    ),
    # A band of facts, which draws its own cells, fills the frame as a table
    # does, the prose after it in the frame's padding; as a project's GitHub
    # panel draws one.
    "part/section-band": Entry(
        '{% section "Example repository" %}<div class="fact-band">'
        '{% band_fact icon="commit" label="Example commit" %}<strong>Example</strong>'
        "<small>An example detail</small>{% endband_fact %}"
        '{% band_fact icon="check-circle" label="Example checks" tone="good" %}<strong>Passing</strong>'
        "<small>An example detail</small>{% endband_fact %}"
        '{% band_fact icon="shield" label="Example alerts" %}<strong>Clear</strong>{% endband_fact %}'
        "</div><p>An example line after the band.</p>{% endsection %}"
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
        '{% section "Example pull requests" count=2 %}<ul class="github-list">'
        '<li><a href="/example/41/">Example change</a> <span class="muted">#41</span></li>'
        '<li><a href="/example/42/">Another example change</a> <span class="muted">#42</span></li>'
        "</ul>{% endsection %}"
        '{% section "Example workflows" %}<p>Example build, example test</p>{% endsection %}'
        "{% endsection %}"
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
        '<div class="dashboard-highlights">{% section "Example overview" css="highlight-card" %}'
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
    # A month with days gone by, each kind of entry on them: their words are
    # still links a reader follows, so they read as text in play does.
    "part/month-gone-by": Entry(
        '{% card css="calendar-month" %}{% include "calendars/_month_grid.html" %}{% endcard %}',
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
        '{% table_heading "Name" local_sort=True %}{% table_heading "State" "status" local_sort=True %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td></tr>'
        '<tr><td>example-02</td><td>{% status "Offline" tone="attention" %}</td></tr></tbody></table>'
        "{% endtable_region %}{% endsection %}"
        '{% section "Example host" %}{% table_region "Example host" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" local_sort=True %}{% table_heading "State" "status" local_sort=True %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td></tr></tbody></table>'
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
    # A status as its words in its tone, among figures, as a change is drawn.
    "status/toned": Entry(
        '{% section "Example changes" %}<p>{% status "+$120.00" tone="good" pill=False %}'
        ' {% status "−$40.00" tone="attention" pill=False %} {% status "−$900.00" tone="serious" pill=False %}'
        ' {% status "$0.00" tone="neutral" pill=False %}</p>{% endsection %}'
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
    "part/notice-info": Entry('{% notice tone="info" %}An example line of news.{% endnotice %}'),
    "part/notice-success": Entry('{% notice tone="success" %}Example saved.{% endnotice %}'),
    "part/notice-error": Entry(
        '{% notice tone="error" %}An example failure long enough to take more than one line on a phone, '
        "so its wrapping is read too.{% endnotice %}"
    ),
    # Among the page's messages, as a post's outcome is said: each a toast.
    "part/notice-messages": Entry(
        '<ul class="messages">{% for tone in tones %}<li>{% notice tone=tone %}An example {{ tone }} message.'
        "{% endnotice %}</li>{% endfor %}</ul>",
        {"tones": ("success", "info", "warning", "error")},
    ),
}


def part_page(name, markup):
    """A page titled ``name``, of HQ's stylesheets, with ``markup`` alone in its main."""

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(name)}</title>{stylesheets()}</head>"
        f'<body><main class="content">{markup}</main></body></html>'
    )


class CatalogLayoutTests(LayoutBrowserTests):
    """Every catalog entry under the whole survey, without script, at the
    sparse widths, which cross the stylesheet's media queries: closed, in both
    palettes, and with every disclosure open."""

    @classmethod
    @override
    def render(cls):
        return {name: part_page(name, entry.drawn(name)) for name, entry in CATALOG.items()}

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
