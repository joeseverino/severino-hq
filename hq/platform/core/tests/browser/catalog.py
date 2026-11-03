"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. A new state of a shared part is
one entry here."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, override
from unittest import mock

from django.template import Context, Template
from django.template.loader import render_to_string
from django.utils.html import escape

from hq.platform.application import calendar
from hq.platform.application.calendar import CalendarEvent, CalendarSource, calendar_month
from hq.platform.application.pages import Page, PageAction
from hq.platform.application.ui import Kpi
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import SPARSE_WIDTHS
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
    # A month with days gone by, each kind of entry on them: their words are
    # still links a reader follows, so they read as text in play does.
    "part/month-gone-by": Entry(
        '{% card css="calendar-month" %}{% include "calendars/_month_grid.html" %}{% endcard %}',
        {"calendar": _month_gone_by()},
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

    def draws_the_state_it_stands_for(self, name):
        if premise := CATALOG[name].premise:
            self.assertEqual(self.read([premise])[0], [], f"{name} does not draw the state it stands for")
