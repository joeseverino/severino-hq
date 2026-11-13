"""The layout rules held over single components, drawn on probe pages and pages of the stress estate."""

import base64
import io
import json
import re
import shutil
import zipfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.template.loader import render_to_string

from hq.platform.application import asks
from hq.platform.application.asks import Ask, Standing
from hq.platform.application.money import money
from hq.platform.application.ui import ActivityCalendar, CalendarDay, ChartSeries, line_chart
from hq.platform.core.stylesheets import DIRECTORY, stylesheet
from hq.platform.core.tests.browser.gate import add_css, served_sheet, tab_key, with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.boxes import ANY_SCROLLER, MAP, SIDEWAYS, SMALL_TARGETS
from hq.platform.core.tests.browser.probes.palette import BUTTON_EDGES
from hq.platform.core.tests.browser.probes.parts import (
    DIALOGS_WITHOUT_OPENERS,
    READINGS,
    SPILLED_LABELS,
    WRAPPED,
    reading_problems,
)
from hq.platform.core.tests.browser.probes.settle import KEPT_SELECTORS, PRESS_EVERY_SHOWN, SETTLE
from hq.platform.core.tests.browser.probes.shifts import CLS_LIMIT, LAYOUT_SHIFTS
from hq.platform.core.tests.browser.probes.tables import (
    BLANK_TABLE,
    CELLS,
    COLUMNS_SHOWN,
    CUT_AND_CLOSED,
    CUT_TABLE,
    CUT_VALUES,
    CUT_WHEN_OPEN,
    HEADING_DROP,
    IDENTIFIER_TABLES,
    IDENTIFIER_VALUES,
    OPEN_ROW_TABLE,
    OPEN_ROW_VALUES,
    PHONE_TABLE,
    POPOVER_IN_A_CELL,
    POPOVER_VALUES,
    PRESS_FIRST_ROW_TOGGLE,
    ROW_ACTIONS_TABLES,
    ROW_ARRIVES,
    ROW_TOGGLE_TABLES,
    ROW_TOGGLES,
    SORT_BY,
    TABLES_THAT_DO_NOT_FIT,
    TITLED_CUTS,
    TITLED_ROWS,
)
from hq.platform.core.tests.browser.probes.text import (
    BROKEN_TOKENS,
    BROKEN_WORDS,
    BROKEN_WORDS_IN_TEXT,
    CHIPS_PAST_THEIR_PAINT,
    IDENTIFIER,
    OVERLAPPING_TEXT,
    TOUCHING_TEXT,
)
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.stress import ALL_PAGES
from hq.platform.core.tests.browser.widths import NARROWEST, PHONE_SCREEN, TARGET_MIN, WIDE, WIDTHS, viewport
from hq.platform.core.tests.test_browser_fixtures import PAGES


def authored_selectors(css: str) -> list[str]:
    """Every style rule's selector as the stylesheet writes it, nested rules
    included: the text before each ``{`` that does not open an at-rule."""

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found, start, quote = [], 0, ""
    for at, char in enumerate(css):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "{};":
            if char == "{":
                header = css[start:at].strip()
                # A keyframe's step is not a selector.
                if header and not header.startswith("@") and not re.fullmatch(r"[\d.%,\s]+|from|to", header):
                    found.append(header)
            start = at + 1
    return found


def _top_level_comma(selector: str) -> bool:
    """Whether ``selector`` is a list of more than one, a comma outside any parentheses."""

    depth = 0
    for char in selector:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and not depth:
            return True
    return False


def _plain_selector(selector: str) -> str:
    """A selector as the browser writes it back: no whitespace, double quotes,
    no ``*`` before what already names an element, and every attribute value
    quoted."""

    plain = re.sub(r"\s+", "", selector).replace("'", '"')
    plain = re.sub(r"\*(?=[:.\[#])", "", plain)
    plain = plain.replace("(odd)", "(2n+1)").replace("(even)", "(2n)")
    return re.sub(r'(\[[^\]=]+?[~|^$*]?=)([^"\]]+)\]', r'\1"\2"]', plain)


# Whether what `selector` names in the line beneath a row is shown.
_SHOWN_BENEATH = "(row, selector) => row.nextElementSibling?.querySelector(selector)?.checkVisibility() ?? false"


# A table whose one cell holds a phrase, which a probe measures beside a
# one-line twin it puts in place and takes away.
_ANSWERING_TABLE = (
    '<div class="table-scroll"><table class="data-table"><thead><tr><th>State</th></tr></thead>'
    '<tbody><tr><td><span class="pill">Example running</span></td></tr></tbody></table></div>'
)

# A page script that squeezes the phrase in a cell whose content changed
# until each word breaks, which the survey would find.
_SQUEEZE_WHAT_CHANGES = """() => new MutationObserver((changes) => changes.forEach(({ target }) => {
  const phrase = target.closest?.('td')?.querySelector('span');
  if (!phrase) return;
  phrase.style.cssText = 'display: inline-block; inline-size: 1ch; white-space: normal; word-break: break-all';
})).observe(document.querySelector('main'), { childList: true, subtree: true })"""

# A page script that counts what it hears.
_COUNT_WHAT_IS_HEARD = """() => {
  window.exampleHeard = 0;
  new MutationObserver((changes) => { window.exampleHeard += changes.length; })
    .observe(document.documentElement, { childList: true, subtree: true });
}"""

# A probe that measures a copy it puts on the page and takes away.
_MEASURE_A_COPY = """() => {
  const copy = document.querySelector('main').appendChild(document.createElement('span'));
  copy.textContent = 'Example';
  const wide = copy.getBoundingClientRect().width;
  copy.remove();
  return wide;
}"""


# How the queue's table stands: whether it is in its narrow box, and the
# width of every action cell shown.
_QUEUE_COLUMNS = """([queue, act]) => {
  const table = document.querySelector(queue);
  return {
    narrow: table.hasAttribute('data-narrow'),
    acts: [...table.querySelectorAll(act)].filter((cell) => cell.checkVisibility())
      .map((cell) => Math.round(cell.getBoundingClientRect().width)),
  };
}"""

# A change of content inside the queue's region, as a fragment or a probe
# makes: a part put in a row's cell and taken away.
_CHANGE_IN_THE_QUEUE = """(cell) => {
  const into = document.querySelector(cell);
  into.append(document.createElement('span'));
  into.lastChild.remove();
}"""

# Every row that offers to open, opened by a way other than its toggle: the
# classes one row gains when its toggle opens it (and loses as it closes)
# set on every such row directly. How many rows it opened.
_OPEN_BY_CLASS = """(toggles) => {
  const shown = [...document.querySelectorAll(toggles)].filter((toggle) => toggle.checkVisibility());
  const row = shown[0].closest('tr');
  const had = new Set(row.classList);
  shown[0].click();
  const gained = [...row.classList].filter((name) => !had.has(name));
  shown[0].click();
  shown.forEach((toggle) => toggle.closest('tr').classList.add(...gained));
  return gained.length ? shown.length : 0;
}"""


# The table a reader scrolls through: the first in a region whose heading
# sticks, with rows enough to scroll past.
_SCROLLED_TABLE = """const table = [...document.querySelectorAll(region)]
    .map((each) => [...each.children].find((child) => child instanceof HTMLTableElement))
    .find((each) => each?.tHead && each.tBodies[0]?.rows.length > 12);
  const rows = [...table.tBodies[0].rows].filter((row) => row.checkVisibility() && !row.hasAttribute('data-row-detail'));
  const heading = table.tHead.rows[0].cells[0];
  const clear = (box) => Math.round(box.getBoundingClientRect().top - heading.getBoundingClientRect().bottom);"""

# A row deep in the table landed on by a link to it, as tables.js lands one:
# how far below the heading the row's top is.
_LAND_ON_A_ROW = (
    """async (region) => {
  """
    + _SCROLLED_TABLE
    + """
  const row = rows[12];
  row.id ||= 'example-landed-row';
  window.location.hash = row.id;
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  return clear(row);
}"""
)

# The first control of the row above, which the window has scrolled past,
# given the focus as the keyboard gives it: how far below the heading it is.
_FOCUS_THE_ROW_ABOVE = (
    """async ([region, target]) => {
  """
    + _SCROLLED_TABLE
    + """
  const control = [...rows[11].querySelectorAll(target)].find((each) => each.checkVisibility());
  control.focus({ focusVisible: true });
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  return clear(control);
}"""
)

# Where a heading that sticks stands in the window, once its table's
# twentieth row is scrolled to the top.
_STUCK_HEADING_TOP = """(region) => {
  const table = [...document.querySelector(region).children].find((child) => child instanceof HTMLTableElement);
  const heading = table.tHead.rows[0].cells[0];
  table.tBodies[0].rows[20].scrollIntoView({ block: 'start' });
  return Math.round(heading.getBoundingClientRect().top);
}"""


# A share row as analytics draws it, half full, added to the page for a shot.
_A_SHARE_ROW = """() => {
  const list = document.createElement('ol');
  list.className = 'bar-list';
  const row = document.createElement('li');
  row.className = 'bar-row';
  const meter = document.createElement('meter');
  meter.className = 'bar-track';
  Object.assign(meter, { min: 0, max: 100, value: 50 });
  const label = document.createElement('span');
  label.className = 'bar-label';
  label.textContent = 'example';
  row.append(meter, label);
  list.append(row);
  document.querySelector('main').prepend(list);
}"""

# Of a shot (a PNG's base64) read down two columns, one inside a half-full
# share's fill and one past it: how many pixel rows the shot has, and in how
# many the two differ, which is how tall the fill is drawn. A meter's parts
# are the engine's own, out of a script's reach, so its pixels are what tell.
_FILL_ROWS = """async (png) => {
  const image = new Image();
  image.src = `data:image/png;base64,${png}`;
  await image.decode();
  const canvas = document.createElement('canvas');
  Object.assign(canvas, { width: image.width, height: image.height });
  const drawn = canvas.getContext('2d');
  drawn.drawImage(image, 0, 0);
  const column = (x) => drawn.getImageData(Math.round(x), 0, 1, image.height).data;
  const inside = column(image.width * 0.4);
  const past = column(image.width * 0.75);
  let filled = 0;
  for (let y = 0; y < image.height; y += 1) {
    if ([0, 1, 2].some((channel) => inside[y * 4 + channel] !== past[y * 4 + channel])) filled += 1;
  }
  return { rows: image.height, filled };
}"""


def others(rows, opened):
    """Every row's height but the one that opened."""

    return rows[:opened] + rows[opened + 1 :]


class ComponentLayoutTests(LayoutBrowserTests):
    """Rules with a setup of their own: one page, probe or control each."""

    def test_with_no_script_every_queue_row_shows_its_face(self):
        """No toggle to press: every row of every family, what is wrong, what
        it means and its buttons are on the page."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("action-items", width)
                rows = self.page.locator(SELECTORS["decisions"])
                self.assertGreater(rows.count(), 0)
                for row in rows.all():
                    for part in ("decision_title", "decision_detail", "decision_actions"):
                        for shown in row.locator(SELECTORS[part]).all():
                            self.assertTrue(shown.is_visible(), part)

    @with_script
    def test_a_queue_row_is_its_words_and_buttons_and_opens_to_its_steps(self):
        """Closed, a row is what is wrong, what it means and how to dismiss it;
        its steps are a press away, beneath it, and a family is a press away
        from its rows."""

        for width in (PHONE_SCREEN[0], WIDTHS[-1]):
            with self.subTest(width=width):
                self.open("action-items", width)
                families = self.page.locator(SELECTORS["decision_family"])
                self.assertGreater(families.count(), 0)
                for family in families.all():
                    family.click()
                rows = self.page.locator(SELECTORS["decisions"])
                opened = 0
                for row in rows.all():
                    self.assertTrue(row.locator(SELECTORS["decision_title"]).is_visible())
                    self.assertTrue(row.locator(SELECTORS["decision_dismiss"]).is_visible())
                    # The row's steps are in its line beneath, drawn by the server.
                    if not row.locator(SELECTORS["decision_steps"]).count():
                        continue

                    def steps_shown(row=row):
                        return row.evaluate(_SHOWN_BENEATH, SELECTORS["decision_steps"])

                    self.assertFalse(steps_shown())
                    row.locator(SELECTORS["row_toggle"]).click()
                    self.assertTrue(steps_shown())
                    row.locator(SELECTORS["row_toggle"]).click()
                    self.assertFalse(steps_shown())
                    opened += 1
                self.assertGreater(opened, 0)

    @with_script
    def test_a_family_opens_to_its_rows_and_closes_them(self):
        """A family's toggle shows the rows folded under it and says so, and a
        second press folds them away again."""

        for width in (PHONE_SCREEN[0], WIDTHS[-1]):
            with self.subTest(width=width):
                self.open("action-items", width)
                toggle = self.page.locator(SELECTORS["decision_family"]).first
                family = toggle.evaluate("(toggle) => toggle.closest('tr').id")
                rows = self.page.locator(f'tr[data-folded="{family}"]')
                self.assertGreater(rows.count(), 0)

                def shown(rows=rows):
                    return [row.is_visible() for row in rows.all()]

                self.assertFalse(any(shown()))
                toggle.click()
                self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
                self.assertTrue(all(shown()))
                toggle.click()
                self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
                self.assertFalse(any(shown()))

    @with_script
    def test_a_folds_rows_offer_their_toggles_as_they_appear(self):
        """The rows a fold shows are measured before the frame that draws
        them, so a row with something cut offers its toggle as it appears."""

        self.pages = {
            **self.pages,
            "fold-probe": probe(
                '{% table_region "Folding" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Name" gutter=True %}{% table_heading "Value" %}</tr></thead><tbody>'
                '{% row_group "Example group" 1 folds="example-fold" %}'
                '<tr id="example-cut" data-folded="example-fold"><td>Name</td>'
                '<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div>'
                "</td></tr></tbody></table>{% endtable_region %}"
            ),
        }
        self.open("fold-probe", PHONE_SCREEN[0])
        offered = self.page.evaluate(
            """async () => {
              document.querySelector('[data-fold-toggle]').click();
              await new Promise((drawn) => requestAnimationFrame(drawn));
              return [...document.querySelectorAll('tr[data-folded] [data-row-toggle]')]
                .filter((toggle) => toggle.checkVisibility()).map((toggle) => toggle.closest('tr').id);
            }"""
        )
        self.assertEqual(offered, ["example-cut"])

    def test_an_events_days_read_as_one_row_and_its_times_as_the_next(self):
        """The day it starts and the day it ends share a row, and the time it
        starts and ends share the one under it, so a range reads across; on a
        phone they stack in the same order."""

        for width in (PHONE_SCREEN[0], WIDE):
            with self.subTest(width=width):
                self.open("event-form", width)
                day, until, start, end = (
                    self.page.get_by_label(re.compile(label)).bounding_box()["y"]
                    for label in (r"^Day\b", r"^Until day\b", r"^From\b", r"^To\b")
                )
                self.assertLess(day, start)
                if width == WIDE:
                    self.assertEqual((until, end), (day, start))
                else:
                    self.assertLess(day, until)
                    self.assertLess(until, start)
                    self.assertLess(start, end)

    def test_a_band_value_that_wraps_stays_in_its_fact_level_with_its_label(self):
        """A fact's linked value, a target tall a line, wraps under its long
        label's row without rising out of the fact, and the label stands
        beside its first line, at every width down to the narrowest."""

        self.pages = {
            **self.pages,
            "band-fact-probe": probe(
                '<h3 class="eyebrow">Example heading over the band</h3>{% facts layout="band" %}'
                '{% fact "An example label long enough to crowd its value" icon="search" %}'
                '<a href="#example">An example value that has to wrap</a>{% endfact %}'
                '{% fact "Example" value="One" icon="shield" %}{% endfacts %}'
            ),
        }
        for width in (NARROWEST, PHONE_SCREEN[0]):
            with self.subTest(width=width):
                self.open("band-fact-probe", width)
                fact, label, lines = self.page.evaluate(
                    """([band, label, value]) => {
                  const fact = document.querySelector(band);
                  const edges = (r) => [r.top, r.bottom];
                  const words = document.createRange();
                  words.selectNodeContents(fact.querySelector(`${value} a`));
                  return [edges(fact.getBoundingClientRect()), edges(fact.querySelector(label).getBoundingClientRect()),
                    [...words.getClientRects()].map(edges)];
                }""",
                    [SELECTORS["band_fact"], SELECTORS["band_label"], SELECTORS["fact_value"]],
                )
                self.assertGreater(len({top for top, _bottom in lines}), 1, "the value wraps")
                self.assertGreaterEqual(min(top for top, _bottom in lines), fact[0])
                self.assertLessEqual(max(bottom for _top, bottom in lines), fact[1])
                first_line = min(lines)
                self.assertLess(label[0], first_line[1])
                self.assertGreater(label[1], first_line[0])

    def test_a_calendars_count_of_planned_days_kept_keeps_its_line_beside_its_legend(self):
        """A legend of three kinds and the missed mark, as a training calendar
        draws it, leaves its count whole on a line at every width."""

        kinds = tuple(ChartSeries(label, (), slot) for slot, label in enumerate(("Running", "Cycling", "Strength"), 1))
        start = date(2026, 1, 5)
        calendar = ActivityCalendar(
            "Example training",
            "Planned days against what was done.",
            tuple(
                tuple(
                    CalendarDay(start + timedelta(days=week * 7 + day), "missed", planned=True)
                    if day in (0, 2, 4) and week * 7 + day < 25
                    else CalendarDay(start + timedelta(days=week * 7 + day))
                    for day in range(7)
                )
                for week in range(5)
            ),
            series=kinds,
            period_label="Example month",
        )
        self.assertEqual(calendar.kept, "0/11")
        self.pages = {
            **self.pages,
            "calendar-probe": probe(render_to_string("partials/_activity_calendar.html", {"calendar": calendar})),
        }
        for width in (NARROWEST, *WIDTHS):
            with self.subTest(width=width):
                self.open("calendar-probe", width)
                self.assertTrue(self.page.locator(SELECTORS["one_line"]).is_visible())
                self.assertEqual(self.page.evaluate(WRAPPED, SELECTORS["one_line"]), [])

    def test_a_chart_tab_shows_which_is_chosen_and_where_the_keyboard_is(self):
        """With no script, the chosen tab is drawn apart from the others and
        its panel alone shows; an arrow key or a press chooses another, the
        tab the keyboard reaches wears the focus ring, and a press draws none."""

        start = date(2026, 1, 5)
        charts = [
            line_chart(
                title,
                "Example readings over a week.",
                ((title, tuple((start + timedelta(days=day), float(day * slot + 3)) for day in range(7)), slot),),
                unit="example",
            )
            for slot, title in enumerate(("Example pace", "Example cadence", "Example power"), 1)
        ]
        self.pages = {
            **self.pages,
            "chart-tabs-probe": probe(
                render_to_string("partials/_chart_tabs.html", {"charts": charts, "group": "example"})
            ),
        }
        drawn = "(tab) => { const s = getComputedStyle(tab); return [s.borderColor, s.backgroundColor, s.color]; }"
        ring = "(tab) => getComputedStyle(tab).outlineStyle"
        self.open("chart-tabs-probe", WIDE)
        tabs = self.page.locator(SELECTORS["chart_tab"])
        panels = self.page.locator(SELECTORS["chart_panel"])

        def chosen():
            looks = [tab.evaluate(drawn) for tab in tabs.all()]
            return [at for at, look in enumerate(looks) if looks.count(look) == 1]

        def shown():
            return [at for at, panel in enumerate(panels.all()) if panel.is_visible()]

        def ringed():
            return [at for at, tab in enumerate(tabs.all()) if tab.evaluate(ring) != "none"]

        self.assertEqual((chosen(), shown(), ringed()), ([0], [0], []))
        # The key for every control (Option-Tab in WebKit, as Safari has it,
        # and Tab elsewhere) reaches the group at its chosen radio, and an
        # arrow chooses the next. WebKit drops :focus-visible when an arrow
        # moves focus within a radio group, so past that the ring may be gone,
        # but it is never left on another tab.
        self.page.keyboard.press(tab_key(self.browser))
        self.assertEqual((chosen(), shown(), ringed()), ([0], [0], [0]))
        self.page.keyboard.press("ArrowRight")
        self.assertEqual((chosen(), shown()), ([1], [1]))
        self.assertLessEqual(set(ringed()), {1})
        tabs.nth(2).click()
        # A tab under the pointer is drawn as hovered, which is not chosen.
        self.page.mouse.move(0, 0)
        self.assertEqual((chosen(), shown(), ringed()), ([2], [2], []))

    def test_each_kind_of_dialog_opener_is_found_for_its_dialog(self):
        """A dialog opened by any opener the modal contract allows is one the
        gates and dev:look can open: a link the page's wiring intercepts, a
        control that names its dialog, and the platform's own invoker, bare."""

        openers = {
            "example-link": '<a href="/" data-modal-open="example-link">Link</a>',
            "example-named": '<a href="/" aria-haspopup="dialog" aria-controls="modal-example-named">Named</a>',
            "example-invoker": '<button type="button" command="show-modal" commandfor="modal-example-invoker">Invoker</button>',
        }
        dialog = '{%% include "partials/_modal.html" with id="%s" title="Example" %%}'
        self.pages = {
            **self.pages,
            "openers-probe": probe("".join(opener + dialog % name for name, opener in openers.items())),
        }
        self.open("openers-probe", WIDE)

        for name in openers:
            with self.subTest(name):
                self.assertEqual(self.page.locator(SELECTORS["dialog_opener"].format(id=name)).count(), 1)
        said = self.page.evaluate(DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]])
        self.assertEqual(said, [])

    def test_about_text_opens_from_the_keyboard_and_gives_focus_back(self):
        """The about button is reached by the key for every control, opens
        its dialog with Enter and no script, and Escape closes it with focus
        back on the button."""

        self.pages = {
            **self.pages,
            "about-probe": probe(
                render_to_string(
                    "partials/_about.html",
                    {"id": "example", "title": "Example notes", "body_template": "partials/_empty_state.html"},
                )
            ),
        }
        self.open("about-probe", WIDE)
        dialog = self.page.locator(SELECTORS["dialog"])
        focused_opener = "() => document.activeElement?.getAttribute('commandfor')"

        self.page.keyboard.press(tab_key(self.browser))
        self.assertEqual(self.page.evaluate(focused_opener), "modal-example")
        self.page.keyboard.press("Enter")
        self.assertTrue(dialog.evaluate("(dialog) => dialog.open"))
        self.page.keyboard.press("Escape")
        self.assertFalse(dialog.evaluate("(dialog) => dialog.open"))
        self.assertEqual(self.page.evaluate(focused_opener), "modal-example")

    @with_script
    def test_a_rows_ask_says_its_sentence_on_the_line_its_row_opens_to(self):
        """A row's Refresh that fails says its word in the row, which keeps
        its height, and its sentence on the row's open line, where a phone
        with no title to hover reads it."""

        # Where the column of actions stays: one that leaves takes its ask to
        # the open line whole, where its note says the sentence itself.
        for width in (820, WIDE):
            with self.subTest(width=width):
                self.open("connections", width)
                pressed = [
                    button
                    for button in self.page.locator(f"{SELECTORS['row_ask']} [data-ask-button]").all()
                    if button.is_visible()
                ]
                self.assertTrue(pressed, "no row's ask to press")
                button = pressed[0]
                row = button.evaluate_handle("(button) => button.closest('tr')").as_element()
                tall = row.bounding_box()["height"]
                button.click()
                self.page.wait_for_selector(f'{SELECTORS["row_ask"]}[data-ask-state="failed"]')
                self.assertLessEqual(row.bounding_box()["height"], tall + 1)
                row.query_selector(SELECTORS["row_toggle"]).click()
                said = row.evaluate(
                    "(row, why) => { const shown = row.nextElementSibling?.querySelector(why);"
                    " return shown?.checkVisibility() ? shown.textContent : null; }",
                    SELECTORS["ask_why"],
                )
                self.assertIsNotNone(said, "the open line says nothing of the ask")
                self.assertIn(button.get_attribute("title"), said)

    @with_script
    def test_a_rows_ask_that_succeeds_drops_the_failure_it_last_said(self):
        """A row whose last refresh failed carries the failure as its button's
        title; pressed again and done, the title says what the button is for."""

        failed = Ask(
            "Refresh",
            "/example/ask/",
            Standing(asks.FAILED, "The example could not be read."),
            title="Read the example again.",
        )
        self.pages = {
            **self.pages,
            "ask-probe": probe(
                '{% table_region "Things" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Name" gutter=True %}{% table_heading "Act" hidden=True %}</tr></thead><tbody>'
                "<tr><td>A</td>"
                '<td>{% include "partials/_ask.html" with ask=ask %}</td></tr></tbody></table>{% endtable_region %}'
                '<form id="hq-post" method="post" hidden></form>'
                '<script defer src="{{ static }}js/fragment.js" nonce="{{ nonce }}"></script>'
                '{% include "partials/_notice_frames.html" %}'
                '<script defer src="{{ static }}js/ask.js" nonce="{{ nonce }}"></script>',
                ask=failed,
            ),
            "example/ask": json.dumps(Standing(asks.DONE).as_json()),
        }
        self.open("ask-probe", WIDE)
        button = self.page.locator(f"{SELECTORS['row_ask']} [data-ask-button]")
        self.assertIn("could not be read", button.get_attribute("title"))
        button.press("Enter")
        self.page.wait_for_selector(f'{SELECTORS["row_ask"]}[data-ask-state="done"]')
        self.assertEqual(button.get_attribute("title"), "Read the example again.")

    @with_script
    def test_an_opened_row_keeps_its_words_in_their_cells(self):
        """Opening a row lifts every cut in it, and what was cut breaks inside
        its column rather than running under the next one."""

        for name in ALL_PAGES:
            for width in (PHONE_SCREEN[0], 820):
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    # A family first, so the rows it folds show their own toggles.
                    self.page.locator(SELECTORS["decision_family"]).evaluate_all(PRESS_EVERY_SHOWN)
                    self.page.locator(SELECTORS["row_toggle"] + ":not([data-fold-toggle])").evaluate_all(
                        PRESS_EVERY_SHOWN
                    )
                    self.page.evaluate(SETTLE)
                    self.assertEqual(self.page.evaluate(CELLS), [])
                    self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

    def test_a_probe_that_throws_hides_no_other(self):
        """Probes read in one round trip each run on their own."""

        self.open(next(iter(PAGES)), WIDE)
        found = self.outcomes([("() => 1", None), ("() => { throw new Error('example'); }", None), ("(n) => n + 1", 2)])
        self.assertEqual([returned for returned, _value in found], [True, False, True])
        self.assertIn("example", found[1][1])
        self.assertEqual(found[2][1], 3)

    @with_script
    def test_every_page_runs_under_the_policy_hq_sends(self):
        """A page HQ answered carries its own policy header into the browser,
        a page a test put together carries HQ's, and a script written into
        the page is refused, the refusal naming its directive and its page."""

        name = next(iter(PAGES))
        response = self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")
        self.assertEqual(
            response.headers["content-security-policy"], self.pages[name].headers["Content-Security-Policy"]
        )
        self.pages = {**self.pages, "inline-script-probe": probe("<script>document.body.dataset.ran = 'yes';</script>")}
        self.open("inline-script-probe", WIDE)
        self.assertIsNone(self.page.evaluate("() => document.body.dataset.ran ?? null"))
        refusals = self.refusals_so_far()
        self.assertTrue(
            [said for said in refusals if "/inline-script-probe/" in said and "script-src" in said], refusals
        )

    def test_a_hit_area_hung_past_a_control_is_no_label_and_a_word_past_it_is(self):
        """The hit area a small control hangs beside itself widens what it
        scrolls, never what it says; a word wider than its control spills."""

        self.open(next(iter(PAGES)), WIDE)
        widened = self.page.evaluate("""() => {
          const star = document.createElement('button');
          star.className = 'pin-toggle example-star';
          star.textContent = '☆';
          const word = document.createElement('button');
          word.className = 'example-word';
          word.textContent = 'example label';
          word.style.setProperty('width', '2rem');
          word.style.setProperty('white-space', 'nowrap');
          document.querySelector('main').append(star, word);
          return star.scrollWidth > star.clientWidth;
        }""")
        self.assertTrue(widened)
        found = self.page.evaluate(SPILLED_LABELS, "main :is(.example-star, .example-word)")
        self.assertEqual([spilled.split(" holds ")[0] for spilled in found], ["button.example-word"])

    @with_script
    def test_a_column_holds_out_for_the_longest_part_its_cut_names_cannot_break(self):
        """A name cut short in a narrow table opens whole in its row, its
        columns held where they were, so its column is already as wide as
        the longest run of the name between hyphens, measured unconstrained,
        where that run is within the column's share."""

        from hq.platform.core.templatetags.value_tags import breakable

        prose = "example words that wrap over many lines of a cell of its own " * 3
        self.pages = {
            **self.pages,
            "cut-name-probe": probe(
                '<div class="table-scroll" style="inline-size: 500px"><table class="data-table"><thead><tr>'
                '<th class="row-gutter">Unit</th><th>Runs</th><th>Result</th></tr></thead><tbody><tr>'
                f'<td class="row-gutter"><code>{breakable("example-examplehost.path")}</code></td>'
                f"<td>{prose}</td><td>{prose}</td></tr></tbody></table></div>"
            ),
        }
        self.open("cut-name-probe", PHONE_SCREEN[0])
        widths = self.page.evaluate(
            """async ([part, scroll]) => {
          const region = document.querySelector(scroll);
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
          // The part alone, in the face the name is drawn in.
          const style = getComputedStyle(region.querySelector('code').lastElementChild);
          const free = document.createElement('span');
          free.textContent = part;
          for (const property of ['font-family', 'font-size', 'font-weight', 'letter-spacing']) {
            free.style.setProperty(property, style.getPropertyValue(property));
          }
          free.style.setProperty('white-space', 'nowrap');
          document.body.append(free);
          const heading = region.querySelector('th');
          const pad = getComputedStyle(heading);
          const inside = heading.clientWidth - parseFloat(pad.paddingLeft) - parseFloat(pad.paddingRight);
          return [inside, free.getBoundingClientRect().width];
        }""",
            ["examplehost.path", SELECTORS["table_scroll"]],
        )
        column, part = widths
        self.assertGreaterEqual(column, part)

    def test_a_popover_open_over_a_table_cell_is_not_held_to_the_cell_and_a_box_in_the_cell_is(self):
        """A shown popover is drawn in the top layer, over the page, wherever
        its markup sits; a box drawn in the cell still runs past it."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const table = document.createElement('table');
          table.style.setProperty('width', '200px');
          table.style.setProperty('table-layout', 'fixed');
          const cell = table.insertRow().insertCell();
          const [popover] = ['example-popover', 'example-box'].map((name) => {
            const box = document.createElement('div');
            box.className = name;
            box.textContent = name;
            box.style.setProperty('inline-size', '600px');
            cell.append(box);
            return box;
          });
          popover.popover = 'manual';
          document.querySelector('main').append(table);
          popover.showPopover();
        }""")
        found = [line for line in self.page.evaluate(CELLS) if "example-" in line]
        self.assertEqual([line.split(" runs ")[0] for line in found], ["div.example-box"])

    def test_what_moves_sideways_is_allowed_only_inside_a_box_the_exemption_names(self):
        """A box past the page's edge is found wherever it is not held by a
        scrolling box the exemption names: under any scroller, one inside a
        lane that scrolls is held and the lane passes; under the boxes built
        to scroll, the lane is found for what it hides and what it holds for
        running past the page. Either way a table's own scroll box run past
        the page is found."""

        self.open(next(iter(PAGES)), NARROWEST)
        self.page.evaluate("""() => {
          const wide = (name) => {
            const box = document.createElement('div');
            box.className = name;
            box.style.cssText = 'inline-size: 200vw; block-size: 1rem';
            return box;
          };
          const lane = document.createElement('div');
          lane.className = 'example-lane';
          lane.style.setProperty('overflow-x', 'auto');
          lane.append(wide('example-in-lane'));
          const table = document.createElement('div');
          table.className = 'table-scroll example-table';
          table.style.cssText = 'overflow-x: auto; inline-size: 200vw; max-inline-size: none; block-size: 1rem';
          document.querySelector('main').append(lane, wide('example-loose'), table);
        }""")

        def found(allowed):
            said = self.page.evaluate(SIDEWAYS, allowed)
            return {line.split(" ")[0] for line in said if "example-" in line}

        self.assertEqual(found(ANY_SCROLLER), {"div.example-loose", "div.table-scroll.example-table"})
        self.assertEqual(
            found(SELECTORS["sideways"]),
            {"div.example-lane", "div.example-in-lane", "div.example-loose", "div.table-scroll.example-table"},
        )

    def test_a_word_wider_than_its_chip_may_break_and_a_short_one_in_a_wide_chip_may_not(self):
        """A run is measured against the box it lays out in: a digest that
        fits its cell but not the padded chip in it breaks wherever the column
        is, while a word that would fit its line, broken anyway, is found."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const chip = (text, style) => {
            const table = document.createElement('table');
            const cell = table.insertRow().insertCell();
            const box = document.createElement('span');
            box.textContent = text;
            box.style.cssText = `display: block; font-family: monospace; ${style}`;
            cell.append(box);
            document.querySelector('main').append(table);
            return [cell, box];
          };
          // As wide as its cell holds, and padded past what the cell's line leaves.
          const [cell, box] = chip('0123456789abcdef0123456789abcdef', 'overflow-wrap: anywhere; padding: 0 10px');
          const range = document.createRange();
          range.selectNodeContents(box.firstChild);
          cell.style.setProperty('padding', '0');
          cell.parentElement.closest('table').style.cssText = `table-layout: fixed; inline-size: ${range.getBoundingClientRect().width + 4}px`;
          chip('an exampleword', 'word-break: break-all; inline-size: 10ch');
        }""")
        found = [
            line
            for line in self.page.evaluate(BROKEN_WORDS, SELECTORS["identifier"])
            if "abcdef" in line or "example" in line
        ]
        self.assertEqual(len(found), 1)
        self.assertIn("example", found[0])

    def test_an_identifier_split_at_its_seams_is_one_run_and_a_plain_word_is_not(self):
        """An identifier wider than its line breaks at its seam and then
        wherever it must, though the seam leaves its last part a text node of
        its own: the run is the identifier. A plain word broken in the same
        line is found."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const table = document.createElement('table');
          table.style.cssText = 'table-layout: fixed; inline-size: 6ch';
          const token = document.createElement('span');
          const seam = document.createElement('span');
          token.className = 'token';
          seam.className = 'seam';
          token.append('plant:', seam, 'somewhere');
          for (const content of [token, 'everywhere']) {
            const cell = table.insertRow().insertCell();
            cell.style.cssText = 'padding: 0; overflow-wrap: anywhere';
            cell.append(content);
          }
          document.querySelector('main').append(table);
        }""")
        found = [line for line in self.page.evaluate(BROKEN_WORDS, SELECTORS["identifier"]) if "where" in line]
        self.assertEqual(len(found), 1, found)
        self.assertIn('"everywhere"', found[0])

    def test_an_open_rows_value_breaks_a_word_only_when_the_whole_line_is_narrower(self):
        """In the line beneath an open row a value left less of the line than
        its label takes breaks a word the whole line holds, and that is found:
        the row shows a value whole. A word longer than the whole line breaks
        where it must."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const table = document.createElement('table');
          const row = table.insertRow();
          const toggle = document.createElement('button');
          toggle.setAttribute('aria-expanded', 'true');
          row.insertCell().append(toggle);
          const detail = table.insertRow();
          detail.dataset.rowDetail = '';
          const pairs = document.createElement('dl');
          pairs.style.cssText = 'display: grid; grid-template-columns: 7rem 4ch; inline-size: 20rem; margin: 0; overflow-wrap: anywhere';
          for (const word of ['Unrecognized', 'Example'.repeat(12)]) {
            const [term, value] = [document.createElement('dt'), document.createElement('dd')];
            term.textContent = 'Label';
            value.textContent = word;
            value.style.margin = '0';
            pairs.append(term, value);
          }
          detail.insertCell().append(pairs);
          document.querySelector('main').append(table);
        }""")
        found = [
            line
            for line in self.page.evaluate(BROKEN_WORDS, SELECTORS["identifier"])
            if "Unrecognized" in line or "Example" in line
        ]
        self.assertEqual(len(found), 1, found)
        self.assertIn('"Unrecognized"', found[0])

    def test_a_long_word_breaks_rather_than_widening_the_page(self):
        """A word too long for its line (a URL, a key, a digest) written in
        plain text breaks where it would overflow, inside the box that holds
        it, so the page never grows sideways to fit it."""

        self.open(next(iter(PAGES)), NARROWEST)
        text, box = self.page.evaluate("""() => {
          const said = document.createElement('p');
          said.textContent = 'example' + '0123456789abcdef'.repeat(8);
          document.querySelector('main').append(said);
          const range = document.createRange();
          range.selectNodeContents(said);
          return [range.getBoundingClientRect().right, said.getBoundingClientRect().right];
        }""")
        self.assertLessEqual(text, box + 1)

    def test_an_amount_holds_to_its_sign_and_one_that_does_not_is_found(self):
        """In a box narrower than any amount, every amount HQ writes stays one
        unit, while a sign written loose before its currency mark ends a line
        and is a finding of the broken-text check."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate(
            """(written) => {
              for (const [name, text] of written) {
                const box = document.createElement('p');
                box.className = name;
                box.textContent = text;
                // Only the breaks the text itself allows: none forced past them.
                box.style.cssText = 'inline-size: 1ch; overflow-wrap: normal; word-break: normal';
                document.querySelector('main').append(box);
              }
            }""",
            [
                ["example-written", f"{money(Decimal('-1243.52'))} {money(Decimal(-5), cents=False)}"],
                ["example-loose", "\u2212$1,243.52"],
            ],
        )

        found = [line for line in self.page.evaluate(BROKEN_WORDS_IN_TEXT, IDENTIFIER) if "example-" in line]
        self.assertEqual(len(found), 1, found)
        self.assertIn("example-loose", found[0])

    def test_a_digest_wider_than_its_line_breaks_where_it_must_and_a_word_never(self):
        """A digest in a sentence (an account's key) is wider than its line, so
        it breaks inside the letters it holds and that is no broken word,
        while a word of the same sentence broken in its box is found."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate(
            """(written) => {
              for (const [name, text, breaks] of written) {
                const box = document.createElement('p');
                box.className = name;
                box.textContent = text;
                box.style.cssText = `inline-size: 12ch; word-break: normal; overflow-wrap: ${breaks}`;
                document.querySelector('main').append(box);
              }
            }""",
            [
                ["example-digest", "Names the account " + "abcdefghijklmnop1" * 8, "anywhere"],
                ["example-word", "Names the account examplelongerthanitsline", "anywhere"],
            ],
        )

        found = [line for line in self.page.evaluate(BROKEN_WORDS_IN_TEXT, IDENTIFIER) if "example-" in line]
        self.assertEqual([line for line in found if "example-digest" in line], [])
        self.assertTrue(any("example-word" in line for line in found), found)

    def test_a_toolbars_menus_wear_a_buttons_edge_and_its_search_a_fields(self):
        """A list's filter menu, toggle and sort are pressed, so in either
        palette none is edged as the search box beside them is, and a control
        given the field's edge is found."""

        self.open("projects", WIDE)
        self.assertGreater(self.page.locator(SELECTORS["table_filter"]).count(), 0)
        for palette in ("light", "dark"):
            with self.subTest(palette=palette):
                self.page.emulate_media(color_scheme=palette)
                self.assertEqual(self.page.evaluate(BUTTON_EDGES, SELECTORS["pressed"]), [])
        self.page.evaluate("""() => {
          const button = document.createElement('button');
          button.className = 'example-edged';
          button.textContent = 'Example';
          button.style.setProperty('border-color', 'var(--field-line)');
          document.querySelector('main').append(button);
        }""")
        self.assertEqual(
            self.page.evaluate(BUTTON_EDGES, SELECTORS["pressed"]), ["button.example-edged wears a field's edge"]
        )

    def test_a_failure_keeps_a_trace_of_the_test_beside_its_screenshot(self):
        """What a failure saves, CI keeps: the page as it stood, and the trace
        of the steps that led there, each with the page as it stood then, in
        the checkout's var/browser-failures/, which outlives the run and is
        the folder CI uploads."""

        self.open(next(iter(PAGES)), WIDE)
        with redirect_stdout(io.StringIO()):
            self.shoot("example-failure")
        folder = self.failures
        if folder is None:
            self.fail("the shot made no folder")
        self.addCleanup(lambda: shutil.rmtree(folder))
        self.assertTrue(folder.is_relative_to(Path(settings.BASE_DIR, "var", "browser-failures")), folder)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["example-failure.png", "example-failure.zip"])
        with zipfile.ZipFile(folder / "example-failure.zip") as trace:
            self.assertTrue(any(name.endswith(".trace") for name in trace.namelist()), trace.namelist())

    def test_a_small_target_under_an_open_menu_is_not_measured_and_one_in_view_is(self):
        """A target drawn under another element is out of a pointer's reach,
        so the size rule skips it; the same target left in view still fails."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const place = (el, top, left) => {
            el.style.setProperty('position', 'fixed');
            el.style.setProperty('top', top);
            el.style.setProperty('left', left);
          };
          // Two side by side, so neither is spaced clear of the other.
          for (const [name, top] of [['example-hidden', '300px'], ['example-shown', '500px']]) {
            for (const left of ['40px', '50px']) {
              const link = document.createElement('a');
              link.href = '#';
              link.className = name;
              link.textContent = 'x';
              link.style.setProperty('font-size', '8px');
              link.style.setProperty('line-height', '8px');
              place(link, top, left);
              document.querySelector('main').append(link);
            }
          }
          const menu = document.createElement('div');
          menu.style.setProperty('width', '200px');
          menu.style.setProperty('height', '100px');
          menu.style.setProperty('z-index', '10');
          menu.style.setProperty('background', 'white');
          place(menu, '260px', '0');
          document.body.append(menu);
        }""")
        found = self.page.evaluate(
            SMALL_TARGETS, ["main :is(.example-hidden, .example-shown)", TARGET_MIN, SELECTORS["floating"]]
        )
        self.assertEqual({finding.split(" in ")[0] for finding in found}, {"a.example-shown"})

    def test_a_stand_in_under_an_open_menu_still_stands_in_for_a_small_target_in_view(self):
        """A target the size of one, covered by a menu drawn over it, is that
        size again once the menu is dismissed, so a small target in view that
        names it as doing the same job passes; one naming a small stand-in
        still fails."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const place = (el, top, left) => {
            el.style.setProperty('position', 'fixed');
            el.style.setProperty('top', top);
            el.style.setProperty('left', left);
          };
          const link = (id, name, top, left, size) => {
            const el = document.createElement('a');
            el.href = '#';
            if (id) el.id = id;
            el.className = name;
            el.textContent = 'x';
            el.style.setProperty('display', 'block');
            el.style.setProperty('width', size);
            el.style.setProperty('height', size);
            place(el, top, left);
            return el;
          };
          const region = document.createElement('section');
          region.append(
            link('example-stand-in', 'example-stand-in', '300px', '40px', '30px'),
            link('example-small-stand-in', 'example-stand-in', '300px', '300px', '10px'),
          );
          // Two small ones side by side, so neither is spaced clear of the other.
          for (const [named, left] of [['example-stand-in', '40px'], ['example-small-stand-in', '300px']]) {
            for (const step of [0, 10]) {
              const small = link('', `example-named-${named.includes('small') ? 'small' : 'sized'}`, '400px', `${parseInt(left) + step}px`, '8px');
              small.dataset.targetEquivalent = named;
              region.append(small);
            }
          }
          document.querySelector('main').append(region);
          const menu = document.createElement('div');
          menu.style.setProperty('width', '400px');
          menu.style.setProperty('height', '60px');
          menu.style.setProperty('z-index', '10');
          menu.style.setProperty('background', 'white');
          place(menu, '285px', '0');
          document.body.append(menu);
        }""")
        found = self.page.evaluate(
            SMALL_TARGETS,
            [
                "main :is(.example-stand-in, .example-named-sized, .example-named-small)",
                TARGET_MIN,
                SELECTORS["floating"],
            ],
        )
        self.assertEqual({finding.split(" in ")[0] for finding in found}, {"a.example-named-small"})

    def test_a_shown_popover_is_a_layer_of_its_own_beside_the_page_beneath(self):
        """A popover's target reaching over the area a page target hangs
        beside its box, short of its center, leaves that area the page
        target's: the two lie in different layers, so neither is the other's
        neighbor. The popover's own small targets still fail."""

        self.open(next(iter(PAGES)), WIDE)
        foot = self.page.evaluate("""() => {
          const place = (el, top, left) => {
            el.style.setProperty('position', 'fixed');
            el.style.setProperty('top', top);
            el.style.setProperty('left', left);
          };
          // 18px tall, hit through the 24px its ::after hangs over its edges.
          const beneath = document.createElement('a');
          beneath.href = '#';
          beneath.className = 'subtle-link example-beneath';
          beneath.textContent = 'Example heading';
          beneath.style.setProperty('font-size', '12px');
          beneath.style.setProperty('line-height', '18px');
          place(beneath, '300px', '40px');
          document.querySelector('main').append(beneath);
          const popover = document.createElement('div');
          popover.popover = 'manual';
          popover.style.setProperty('inset', 'auto');
          popover.style.setProperty('margin', '0');
          popover.style.setProperty('padding', '0');
          place(popover, '200px', '40px');
          const cover = document.createElement('button');
          cover.type = 'button';
          cover.className = 'example-cover';
          cover.textContent = 'Example cover';
          cover.style.setProperty('display', 'block');
          cover.style.setProperty('box-sizing', 'border-box');
          cover.style.setProperty('width', '200px');
          cover.style.setProperty('height', '30px');
          cover.style.setProperty('margin', '40px 0 0');
          const pair = document.createElement('div');
          pair.style.setProperty('position', 'absolute');
          pair.style.setProperty('top', '8px');
          pair.style.setProperty('left', '0');
          for (const left of ['0', '10px']) {
            const link = document.createElement('a');
            link.href = '#';
            link.className = 'example-within';
            link.textContent = 'x';
            link.style.setProperty('position', 'absolute');
            link.style.setProperty('left', left);
            link.style.setProperty('font-size', '8px');
            link.style.setProperty('line-height', '8px');
            pair.append(link);
          }
          popover.append(pair, cover);
          document.body.append(popover);
          popover.showPopover();
          // Its foot 1px above the heading's box, in the 3px its ::after hangs there.
          const foot = () => cover.getBoundingClientRect().bottom - beneath.getBoundingClientRect().top;
          place(popover, `${200 - foot() - 1}px`, '40px');
          return foot();
        }""")
        self.assertEqual(foot, -1)
        found = self.page.evaluate(
            SMALL_TARGETS, [":is(.example-beneath, .example-cover, .example-within)", TARGET_MIN, SELECTORS["floating"]]
        )
        self.assertEqual({finding.split(" in ")[0] for finding in found}, {"a.example-within"})

    def test_a_modal_dialog_leaves_the_page_beneath_unmeasured_and_its_own_targets_measured(self):
        """A modal dialog makes the rest of the document inert, so no
        pointer reaches a target behind it wherever it lies; the dialog's own
        small targets still fail."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const pair = (parent, name) => {
            for (const left of ['40px', '50px']) {
              const link = document.createElement('a');
              link.href = '#';
              link.className = name;
              link.textContent = 'x';
              link.style.setProperty('position', 'fixed');
              link.style.setProperty('top', '8px');
              link.style.setProperty('left', left);
              link.style.setProperty('font-size', '8px');
              link.style.setProperty('line-height', '8px');
              parent.append(link);
            }
          };
          pair(document.querySelector('main'), 'example-beneath');
          const dialog = document.createElement('dialog');
          document.body.append(dialog);
          pair(dialog, 'example-within');
          dialog.showModal();
        }""")
        found = self.page.evaluate(
            SMALL_TARGETS, [":is(.example-beneath, .example-within)", TARGET_MIN, SELECTORS["floating"]]
        )
        self.assertEqual({finding.split(" in ")[0] for finding in found}, {"a.example-within"})

    def test_an_open_menu_over_the_page_text_is_no_overlap_and_text_in_one_layer_is(self):
        """An open menu's panel covers the page's text on purpose; two lines
        drawn over each other in the page, or in the one panel, still overlap."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate("""() => {
          const line = (text, top, left) => {
            const p = document.createElement('p');
            p.textContent = text;
            for (const [name, value] of [['position', 'fixed'], ['top', top], ['left', left], ['margin', '0']]) {
              p.style.setProperty(name, value);
            }
            return p;
          };
          const menu = document.createElement('div');
          menu.dataset.menu = '';
          const panel = document.createElement('div');
          panel.popover = 'manual';
          panel.append(line('example covering', '300px', '40px'), line('example first', '400px', '40px'), line('example second', '400px', '40px'));
          menu.append(panel);
          const main = document.querySelector('main');
          main.append(line('example covered', '300px', '40px'), menu, line('example clashed', '500px', '40px'));
          main.append(line('example under', '500px', '40px'));
          panel.showPopover();
        }""")
        found = self.page.evaluate(OVERLAPPING_TEXT, SELECTORS["floating"])
        pairs = {tuple(sorted(part.split('"')[1] for part in finding.split(" over "))) for finding in found}
        self.assertNotIn(("example covered", "example covering"), pairs)
        self.assertIn(("example clashed", "example under"), pairs)
        self.assertIn(("example first", "example second"), pairs)

    def test_text_set_flush_against_another_element_s_text_touches_it_and_a_word_split_at_its_seams_does_not(self):
        """A heading and its status as items of a flex row lose the space
        between them in the markup and read as one word; the same pair with
        a space drawn between them, and a name ``breakable`` split into
        boxes at its seams, are each one run of words. Two marks each centered
        in a button of its own, a gap between the buttons, stand apart, as
        the page's rules for what the buttons hold size them."""

        from hq.platform.core.templatetags.value_tags import breakable

        self.pages = {
            **self.pages,
            "touching-probe": probe(
                '<div style="display: flex; align-items: baseline"><h2 style="margin: 0">Example heading</h2> '
                "<span>Example touching</span></div>"
                '<div><h2 style="display: inline-block; margin: 0">Example spaced</h2> '
                '<span style="display: inline-block">Example status</span></div>'
                f'<p class="example-seams">{breakable("status.example-host-name.example.com")}</p>'
                '<nav class="example-buttons" style="display: flex; gap: 4px">'
                + "".join(f'<a href="#">{mark}</a>' for mark in "xy")
                + "</nav>"
            ),
        }
        self.open("touching-probe", WIDE)
        add_css(self.page, ".example-seams .token > * { display: inline-block; }")
        add_css(
            self.page,
            ".example-buttons :is(a, span) { display: grid; place-items: center; inline-size: 28px; block-size: 28px; font-size: 20px; }",
        )
        found = self.page.evaluate(TOUCHING_TEXT, [SELECTORS["floating"], SELECTORS["identifier"]])
        said = " ".join(found)
        self.assertIn('"Example heading"', said)
        self.assertNotIn('"Example spaced"', said)
        for part in ("status.", "example-host-name", '"y"'):
            self.assertNotIn(part, said)

    def test_a_chip_draws_its_text_on_what_it_paints(self):
        """A chip whose words are laid out past the box it paints is found,
        and the tailnet's port chips, each with the name of what answers on
        it, draw every word on their chip."""

        from hq.platform.core.templatetags.value_tags import breakable

        self.pages = {
            **self.pages,
            "chip-probe": probe(
                '<p><code style="display: inline-block; inline-size: 4ch; block-size: 1lh; background: #eee">'
                f"{breakable('Example overrun')}</code></p>"
            ),
        }
        arg = [SELECTORS["chip"], SELECTORS["unseen"]]
        self.open("chip-probe", WIDE)
        self.assertTrue(self.page.evaluate(CHIPS_PAST_THEIR_PAINT, arg))
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("dense/tailnet", width)
                self.assertEqual(self.page.evaluate(CHIPS_PAST_THEIR_PAINT, arg), [])

    def test_every_selector_parses(self):
        """Every rule the stylesheet writes is one the engine keeps, but for
        another engine's own part (``::-moz-progress-bar`` in WebKit) in a
        rule of its own: an engine drops a whole rule whose list names a part
        it does not know, so such a part shares its rule with nothing."""

        authored = authored_selectors(stylesheet())
        self.open(next(iter(PAGES)), WIDTHS[-1])
        kept = {_plain_selector(selector) for selector in self.page.evaluate(KEPT_SELECTORS, f"/{DIRECTORY}/")}
        own = "-moz-" if self.browser.browser_type.name == "firefox" else "-webkit-"

        def anothers_part_alone(selector):
            parts = set(re.findall(r"::(-(?:moz|webkit)-)", selector))
            return bool(parts) and own not in parts and not _top_level_comma(selector)

        dropped = [selector for selector in authored if _plain_selector(selector) not in kept]
        self.assertEqual([selector for selector in dropped if not anothers_part_alone(selector)], [])

    def test_a_share_fills_its_row_from_edge_to_edge(self):
        """A share's fill is as tall as its row, in every engine (`.bar-track`
        in pages.css)."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate(_A_SHARE_ROW)
        # The page's own shot, which drops the style WebKit adds to take one.
        shot = self.page.screenshot(clip=self.page.locator(SELECTORS["share_bar"]).first.bounding_box())
        fill = self.page.evaluate(_FILL_ROWS, base64.b64encode(shot).decode())
        self.assertGreater(fill["rows"], 10)
        self.assertGreaterEqual(fill["filled"], fill["rows"] - 1, fill)

    def test_the_dashboard_pairs_its_cards_on_a_wide_screen(self):
        self.open("dashboard", 1280)
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assertGreater(max(len(row) for row in self.card_rows(key)), 1)

    def test_the_dashboard_stacks_its_cards_on_a_phone(self):
        for width in (NARROWEST, PHONE_SCREEN[0]):
            self.open("dashboard", width)
            for key in ("highlights", "patterns"):
                with self.subTest(cards=key, width=width):
                    rows = self.card_rows(key)
                    self.assertGreater(len(rows), 1)
                    self.assertEqual({len(row) for row in rows}, {1})
                    self.assertEqual({round(row[0]["left"]) for row in rows}, {round(rows[0][0]["left"])})

    @with_script
    def test_a_row_toggle_is_owed_only_to_what_a_reader_cannot_see(self):
        """Every row the server made openable has its toggle in the HTML, in
        the cell that names it, and shows it only while the row holds
        something back: a column that left a table wider than its box, with
        something in it, a part of a list it folds, or a value cut short,
        which opens its row only where the gutter is kept already. A narrow
        box keeps it for every table whose rows open, since touch has no
        hover title; a wide one only for a table with something else to open. A dash in a dropped column says
        nothing, screen-reader text is clipped to a pixel by design and a
        decoration is hidden from assistive technology: none is content a row
        withholds. Whether a value is cut is measured, so script is on.
        """

        self.pages = {**self.pages, "row-toggle-probe": probe(ROW_TOGGLE_TABLES)}
        shown_at = {
            PHONE_SCREEN[0]: {"dropped", "cut", "folded", "cut-in-a-kept-gutter"},
            1280: {"folded", "cut-in-a-kept-gutter"},
        }
        for width, expected in shown_at.items():
            with self.subTest(width=width):
                self.open("row-toggle-probe", width)
                self.page.evaluate(SETTLE)
                rows = self.page.evaluate(ROW_TOGGLES)
                self.assertEqual({(row["toggles"], row["beside_name"]) for row in rows}, {(1, True)})
                self.assertEqual({row["probe"] for row in rows if row["shown"]}, expected)

    @with_script
    def test_a_cut_value_reads_whole_where_its_row_opens_or_a_wide_box_titles_it(self):
        """A cut value is shown whole by its row opening, or outside a narrow
        box by its title on hover, which tables.js gives a cut value that has
        none; touch has no hover, so in a narrow box only an opening row
        shows it, and a cut there in a row that cannot open is found. A cut
        that draws no ellipsis (a clamp's counts) is found at any width,
        title or not, since nothing tells a reader there is more to hover
        for; a name's chip in a row's muted line draws its own, and a narrow
        box breaks it whole. A cut with no title outside a narrow box is the
        survey's plant."""

        self.pages = {**self.pages, "cut-probe": probe(CUT_TABLE, **CUT_VALUES)}
        rows = ("untitled", "titled", "opens", "clamped", "inline", "unmarked", "chip")
        for width, expected in {
            PHONE_SCREEN[0]: {"untitled", "titled", "clamped", "inline", "unmarked"},
            1280: {"unmarked"},
        }.items():
            with self.subTest(width=width):
                self.open("cut-probe", width)
                self.page.evaluate(SETTLE)
                found = self.page.evaluate(CUT_AND_CLOSED, SELECTORS["unseen"])
                named = {row for row in rows if any(f'"{row}' in line for line in found)}
                self.assertEqual(named, expected, found)

    @with_script
    def test_an_open_row_shows_every_value_whole(self):
        """Opening a row is how a narrow box shows a value whole, so nothing
        in an open row, nor in the line beneath it where a column that left
        stands, is cut, at any width."""

        self.pages = {**self.pages, "open-row-probe": probe(OPEN_ROW_TABLE, **OPEN_ROW_VALUES)}
        for width in (PHONE_SCREEN[0], WIDE):
            with self.subTest(width=width):
                self.open("open-row-probe", width)
                self.page.evaluate(SETTLE)
                # Every row opened, as a link to it opens one whose toggle does not show.
                self.page.locator("main " + SELECTORS["row_toggle"]).evaluate_all(
                    "(rows) => rows.forEach((row) => row.click())"
                )
                self.assertEqual(self.page.evaluate(CUT_WHEN_OPEN, SELECTORS["unseen"]), [])

    @with_script
    def test_a_cut_value_keeps_its_title_only_while_it_is_cut_and_its_row_closed(self):
        """tables.js titles a cut value where its row's toggle does not show,
        and takes the title away once the value is whole (its box widened)
        and once its row opens, so no title outlives its cut; closed again,
        the row's value is titled again."""

        self.pages = {**self.pages, "titled-probe": probe(TITLED_CUTS)}
        self.open("titled-probe", PHONE_SCREEN[0])
        self.page.evaluate(SETTLE)
        self.assertIn("narrows", self.page.evaluate(TITLED_ROWS))

        self.page.set_viewport_size(viewport(WIDE))
        self.page.wait_for_function(f"() => ({TITLED_ROWS})().join() === 'opens'")

        toggle = self.page.locator("main " + SELECTORS["row_toggle"])
        toggle.evaluate("(button) => button.click()")
        self.page.evaluate(SETTLE)
        self.assertEqual(self.page.evaluate(TITLED_ROWS), [])
        toggle.evaluate("(button) => button.click()")
        self.page.evaluate(SETTLE)
        self.assertEqual(self.page.evaluate(TITLED_ROWS), ["opens"])

    @with_script
    def test_a_table_that_arrives_after_the_page_marks_its_cuts(self):
        """A table a fragment brings in once the page has loaded is measured
        as it arrives, so a cut value in a row whose toggle does not show
        reads whole on hover there as on a table the page was drawn with."""

        later = probe(f"<template>{TITLED_CUTS}</template>")
        self.pages = {**self.pages, "later-probe": later}
        self.open("later-probe", WIDE)
        self.page.evaluate("""async () => {
          const later = document.querySelector('main > template');
          later.replaceWith(later.content.cloneNode(true));
          await new Promise((done) => requestAnimationFrame(done));
        }""")
        self.assertEqual(self.page.evaluate(TITLED_ROWS), ["opens"])

    @with_script
    def test_a_panel_a_cell_shows_shows_its_values_whole(self):
        """A panel opened from a cell, drawn over the page, is no part of the
        column, so an identifier in it reads whole however narrow the cell."""

        self.pages = {**self.pages, "popover-probe": probe(POPOVER_IN_A_CELL, **POPOVER_VALUES)}
        for width in (PHONE_SCREEN[0], WIDE):
            with self.subTest(width=width):
                self.open("popover-probe", width)
                self.page.evaluate("() => document.querySelector('main [popover]').showPopover()")
                self.assertEqual(self.page.evaluate(CUT_AND_CLOSED, SELECTORS["unseen"]), [])

    @with_script
    def test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing(self):
        """A table of four columns or more keeps the column that names each row
        and the ones marked key, and fits the screen; it does not stack into
        cards and is not swiped through. What left is listed when a row is
        opened, beneath that row, and opening it changes no column's width and
        no other row's height: the reader's place in the table holds. Closed
        again, the line beneath names nothing.
        """

        for name in ("connections", "services", "containers", "projects"):
            with self.subTest(page=name):
                self.open(name, PHONE_SCREEN[0])
                closed = self.page.evaluate(PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertGreaterEqual(closed["headings"], 4)
                self.assertLess(len(closed["columns"]), closed["headings"])
                self.assertLessEqual(closed["sideways"], 1)
                self.assertGreater(closed["toggles"], 0)

                pressed = self.page.evaluate(PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                opened = self.page.evaluate(PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertTrue(opened["details"])
                self.assertEqual(opened["columns"], closed["columns"])
                self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))
                self.assertLessEqual(opened["sideways"], 1)

                self.page.evaluate(PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                again = self.page.evaluate(PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertEqual(again["details"], [])
                self.assertEqual(again["columns"], closed["columns"])
                self.assertEqual(again["rows"], closed["rows"])

    @with_script
    def test_opening_every_row_a_reader_can_open_moves_nothing_they_did_not_ask_for(self):
        """A fold and a row's toggle pressed as a reader presses them push
        what is below the rows they open, which the browser counts as the
        reader's own doing; nothing else moves, at any width, so the page
        stays under `CLS_LIMIT`."""

        read = [SELECTORS["vendored"], CLS_LIMIT]
        folds = "main " + SELECTORS["row_toggle"] + "[data-fold-toggle]"
        rows = "main " + SELECTORS["row_toggle"] + ":not([data-fold-toggle])"
        for name in ("dense/action-items", "dense/containers"):
            for width in WIDTHS:
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    self.settle()
                    for toggles in (folds, rows):
                        shown = self.page.locator(toggles).evaluate_all(
                            "(all) => all.map((toggle, place) => toggle.checkVisibility() ? place : -1)"
                            ".filter((place) => place >= 0)"
                        )
                        for place in shown:
                            self.page.locator(toggles).nth(place).click()
                        self.settle()
                    self.assertTrue(self.page.locator(rows + '[aria-expanded="true"]').count())
                    self.assertEqual(self.page.evaluate(LAYOUT_SHIFTS, read), [])

    @with_script
    def test_a_row_that_arrives_after_its_table_is_fitted_leaves_out_the_columns_that_left(self):
        """The parser hands a long table its rows in pieces, and the table is
        fitted as each piece arrives: a row added once columns have left
        leaves them out too, so it is no wider or taller than the rows
        before it."""

        self.open("containers", PHONE_SCREEN[0])
        fitted = self.page.evaluate(PHONE_TABLE, SELECTORS["table_scroll"])
        self.assertLess(len(fitted["columns"]), fitted["headings"])
        self.assertEqual(self.page.evaluate(ROW_ARRIVES, SELECTORS["table_scroll"]), [])

    @with_script
    def test_a_fragment_that_is_not_percent_encoding_breaks_no_table(self):
        """`#%` is a legal fragment and no encoding of a row's id: it names no
        row, and everything the table script sets up after landing still runs."""

        self.page.goto(f"{ORIGIN}/projects/#%", wait_until="load")
        ready = self.page.eval_on_selector_all(
            SELECTORS["selectable_table"], "(tables) => tables.map((table) => table.dataset.selectionReady)"
        )
        self.kept_the_policy()
        self.assertTrue(ready)
        self.assertEqual(set(ready), {"true"})

    @with_script
    def test_a_wide_table_on_a_desktop_keeps_every_column_and_holds_them_when_a_row_opens(self):
        self.open("connections", 1440)
        closed = self.page.evaluate(PHONE_TABLE, SELECTORS["table_scroll"])
        self.assertEqual(len(closed["columns"]), closed["headings"])

        if closed["toggles"]:
            pressed = self.page.evaluate(PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
            opened = self.page.evaluate(PHONE_TABLE, SELECTORS["table_scroll"])
            self.assertEqual(opened["columns"], closed["columns"])
            self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))

    @with_script
    def test_a_table_with_rows_open_is_fitted_as_it_stands_with_them_closed(self):
        """A table without headings, its rows opened by their toggles or by
        any other way, keeps the columns it had closed when content inside
        its region changes: it stays in its narrow box, and no action cell
        widens around what an open row shows whole."""

        toggles = SELECTORS["row_toggle"] + ":not([data-fold-toggle])"
        for width in (PHONE_SCREEN[0], 820):
            for how in ("toggle", "class"):
                with self.subTest(width=width, opened_by=how):
                    self.open("dense/action-items", width)
                    self.page.locator(SELECTORS["decision_family"]).evaluate_all(PRESS_EVERY_SHOWN)
                    self.page.evaluate(SETTLE)
                    columns = [SELECTORS["queue_table"], SELECTORS["queue_act"]]
                    closed = self.page.evaluate(_QUEUE_COLUMNS, columns)
                    self.assertTrue(closed["acts"])
                    if how == "toggle":
                        self.page.locator(toggles).evaluate_all(PRESS_EVERY_SHOWN)
                        self.assertTrue(self.page.locator(toggles + '[aria-expanded="true"]').count())
                    else:
                        self.assertTrue(self.page.evaluate(_OPEN_BY_CLASS, toggles))
                    self.page.evaluate(_CHANGE_IN_THE_QUEUE, SELECTORS["queue_what"])
                    self.page.evaluate(SETTLE)
                    self.assertEqual(self.page.evaluate(_QUEUE_COLUMNS, columns), closed)

    @with_script
    def test_a_row_scrolled_to_the_top_stands_below_the_heading_that_sticks(self):
        """A row a link lands on, and a control in the row above it given the
        focus, are scrolled to below the table's heading, which sticks under
        the header: neither is drawn beneath it."""

        for width in (PHONE_SCREEN[0], WIDE):
            with self.subTest(width=width):
                self.open("dense/containers", width)
                self.assertGreaterEqual(self.page.evaluate(_LAND_ON_A_ROW, SELECTORS["table_scroll"]), 0)
                above = self.page.evaluate(_FOCUS_THE_ROW_ABOVE, [SELECTORS["table_scroll"], SELECTORS["target"]])
                self.assertGreaterEqual(above, 0)

    @with_script
    def test_a_heading_sticks_at_the_top_of_a_page_with_no_header(self):
        """The inset a heading sticks under is the chrome the page draws, so
        on a page without the header a stuck heading is at the window's top,
        over no row that a header would have hidden."""

        rows = "".join(f"<tr><td>Example {index}</td></tr>" for index in range(60))
        self.pages = {
            **self.pages,
            "headerless-probe": probe(
                '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}</tr></thead>'
                f"<tbody>{rows}</tbody></table>{{% endtable_region %}}"
            ),
        }
        self.open("headerless-probe", WIDE)
        self.assertEqual(self.page.evaluate(_STUCK_HEADING_TOP, SELECTORS["table_scroll"]), 0)

    @with_script
    def test_any_table_sorts_by_a_heading(self):
        """A table whose server does not sort it sorts on the page: ascending,
        then descending, then as it came, each section keeping its own rows.
        """

        self.open("services", 1440)
        came = self.page.evaluate(
            "(scroll) => [...document.querySelectorAll('main ' + scroll + ' > table tbody')]"
            ".map((body) => [...body.querySelectorAll(':scope > tr')]"
            ".filter((row) => !row.querySelector('[colspan]'))"
            ".map((row) => row.children[0].textContent.trim().replace(/\\s+/g, ' ')))",
            SELECTORS["table_scroll"],
        )
        up = self.page.evaluate(SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(up["sort"], "ascending")
        self.assertEqual(up["values"], [sorted(group, key=str.lower) for group in came])
        down = self.page.evaluate(SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(down["sort"], "descending")
        self.assertEqual(down["values"], [sorted(group, key=str.lower, reverse=True) for group in came])
        back = self.page.evaluate(SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(back["sort"], "none")
        self.assertEqual(back["values"], came)

    def test_the_dashboards_readings_show_every_line_beside_its_mark(self):
        """Each is a mark, a lead and its lines: nothing set under the mark,
        where a caption in the mark's narrow column reads as "Via h", and no
        line cut short."""

        for width in (PHONE_SCREEN[0], 820, 1280, 1440):
            with self.subTest(width=width):
                self.open("dashboard", width)
                readings = self.page.locator(SELECTORS["readings"]).evaluate_all(READINGS)
                self.assertGreaterEqual(len(readings), 3)
                self.assertEqual(reading_problems(readings), [])

    @with_script
    def test_what_the_dashboard_says_of_its_readings_covers_none_of_them(self):
        """Opened while a reading is due, the dashboard asks for it and says it
        is waiting for the controller. That line stands where a trail would,
        and the dashboard's readings stand there instead: it is said on a line
        no other text is drawn on, whichever row the readings take."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("dense/dashboard", width)
                self.page.locator(SELECTORS["ask_note"]).wait_for(state="visible")
                self.assertEqual(self.page.evaluate(OVERLAPPING_TEXT, SELECTORS["floating"]), [])

    def test_the_map_scrolls_with_the_page_and_draws_no_empty_lane(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("topology", width)
                found = self.page.evaluate(MAP, [SELECTORS["map"], SELECTORS["map_lane"], SELECTORS["map_node"]])
                self.assertIsNotNone(found)
                self.assertFalse(found["scrolls"])
                self.assertEqual(found["empty"], [])

    def test_a_palette_only_the_systems_preference_reaches_is_caught(self):
        """A rule written under `prefers-color-scheme` paints a page whose
        operator chose the other palette as the system's, which the rule on
        chosen themes names; without it the same page passes."""

        name = next(iter(PAGES))
        self.open(name, WIDE)
        was = self.scheme()
        self.a_chosen_theme_paints_as_the_systems(name)
        # Linked, so the page loaded again from its own HTML keeps it.
        self.page.evaluate(
            "(href) => document.head.append(Object.assign(document.createElement('link'), { rel: 'stylesheet', href }))",
            served_sheet("@media (prefers-color-scheme: dark) { body { background: rgb(1, 2, 3) } }"),
        )
        with self.assertRaises(AssertionError) as caught:
            self.a_chosen_theme_paints_as_the_systems(name)
        for chosen in ("dark", "light"):
            self.assertIn(f"{chosen} chosen paints body", str(caught.exception))
        self.assertEqual(self.scheme(), was)

    @with_script
    def test_a_page_that_never_loads_again_fails_the_chosen_theme_naming_it(self):
        """A reload that never reaches its load event fails the rule within
        its bound, naming the page, rather than holding the gate."""

        name = next(iter(PAGES))
        self.open(name, WIDE)
        # Every script asked for from here on waits unanswered, and the load
        # event waits for them.
        self.held, self.reload_within = [], 500
        with self.assertRaises(AssertionError) as caught:
            self.a_chosen_theme_paints_as_the_systems(name)
        self.assertIn(f"{name} did not load again", str(caught.exception))

    @with_script
    def test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split(self):
        self.pages = {**self.pages, "blank-column-probe": probe(BLANK_TABLE)}
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("blank-column-probe", width)
                self.page.evaluate(SETTLE)
                shown = self.page.evaluate(COLUMNS_SHOWN)
                self.assertNotIn("Nothing", shown)
                self.assertIn("Title", shown)
                self.assertIn("Runs", shown)
                self.assertEqual(self.page.evaluate(BROKEN_WORDS, SELECTORS["identifier"]), [])

    @with_script
    def test_a_rows_controls_stay_in_its_card_and_a_table_that_cannot_fit_scrolls(self):
        """On the narrowest phone a row's controls, each label whole, leave
        with their column for the line beneath the row, so their table fits
        its card; a table nothing it gives can fit scrolls in its region, its
        heading on its first row, and nothing on the page runs past it
        sideways."""

        self.pages = {**self.pages, "row-actions-probe": probe(ROW_ACTIONS_TABLES)}
        self.open("row-actions-probe", NARROWEST)
        self.page.evaluate(SETTLE)
        misfits = self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"])
        # One table, the one that scrolls below: a region draws no landmark, so
        # the probe names a table by its heading.
        self.assertEqual(len(misfits), 1, misfits)
        region = "(probe) => getComputedStyle(document.querySelector(`[data-probe=${probe}]`).parentElement).overflowX"
        self.assertEqual(self.page.evaluate(region, "unfittable"), "auto")
        self.assertEqual(self.page.evaluate(region, "actions"), "visible")
        self.assertEqual(self.page.evaluate(SIDEWAYS, SELECTORS["sideways"]), [])
        self.assertEqual(self.page.evaluate(HEADING_DROP, "unfittable"), 0)

    @with_script
    def test_an_identifier_in_a_table_is_whole_while_a_column_can_leave(self):
        """An address with a port, a host name and a network in a cell are
        never broken at their seams while the table has a ranked column to
        let go: on the narrowest phone the columns leave and the names stay
        whole, and the table still fits its box."""

        self.pages = {**self.pages, "identifier-probe": probe(IDENTIFIER_TABLES, **IDENTIFIER_VALUES)}
        for width in (NARROWEST, *WIDTHS):
            with self.subTest(width=width):
                self.open("identifier-probe", width)
                self.page.evaluate(SETTLE)
                self.assertEqual(
                    self.page.evaluate(BROKEN_TOKENS, [SELECTORS["cell_token"], SELECTORS["wraps_in_a_cell"]]), []
                )
                self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])
        self.open("identifier-probe", NARROWEST)
        self.page.evaluate(SETTLE)
        shown = self.page.evaluate(COLUMNS_SHOWN)
        self.assertNotIn("Read from", shown)
        self.assertNotIn("Logins", shown)

    @with_script
    def test_a_name_a_cell_cuts_keeps_a_pixel_to_spare_on_its_line_where_its_cell_has_room(self):
        """A one-token name cut by a box of its own, in a cell with room to
        spare, draws every part on its one line uncut, with a pixel to spare
        past them: a track sized to the parts' measure alone is short by the
        fraction an engine can mismeasure them by, and then the last part
        leaves the line and only its ellipsis shows. WebKit lays a clamped
        box (`-webkit-box`) out as the legacy box it measures, which keeps
        its parts on their line however close it comes, so there only the
        parts are read."""

        from hq.platform.core.templatetags.value_tags import breakable

        names = [f"/opt/apps/{name}" for name in ("example-connect", "example-id", "example-a1")]
        cells = [
            *(breakable(name) for name in names),
            *(f'<span class="cell-line"><span class="cut">{breakable(name)}</span></span>' for name in names),
            *(f'<a data-entity href="/example/">{breakable(name)}</a>' for name in names),
        ]
        rows = "".join(f"<tr><td>{cell}</td><td>Example</td></tr>" for cell in cells)
        self.pages = {
            **self.pages,
            "name-room-probe": probe(
                '<div class="table-scroll"><table class="data-table"><thead><tr><th>Directory</th><th>State</th></tr>'
                f"</thead><tbody>{rows}</tbody></table></div>"
            ),
        }
        short = """([boxes, sole, whole]) => {
          const found = [];
          const range = document.createRange();
          for (const box of document.querySelectorAll(boxes)) {
            if (getComputedStyle(box.parentElement).display !== 'inline-grid') continue;
            const token = box.closest(sole) ?? box.querySelector(sole);
            const top = box.getClientRects()[0].top;
            const parts = [...box.querySelectorAll(whole)];
            const pad = getComputedStyle(box);
            range.selectNodeContents(box);
            const inside = box.clientWidth - parseFloat(pad.paddingLeft) - parseFloat(pad.paddingRight);
            const spare = inside - range.getBoundingClientRect().width;
            const off = parts.some((part) => part.getBoundingClientRect().top > top + 1 || part.scrollWidth > part.clientWidth);
            if (off || (pad.display !== '-webkit-box' && spare < 0.75))
              found.push(`${token.textContent} spares ${spare.toFixed(2)}px as ${pad.display}`);
          }
          return found;
        }"""
        for scale in (1, 2):
            with self.subTest(scale=scale):
                self.start(java_script_enabled=True, device_scale_factor=scale)
                self.open("name-room-probe", WIDE)
                self.page.evaluate(SETTLE)
                self.assertEqual(
                    self.page.evaluate(short, [SELECTORS["name_cut"], SELECTORS["sole_token"], SELECTORS["part"]]), []
                )

    @with_script
    def test_a_table_whose_content_widens_after_its_fit_gives_way_again(self):
        """A table fitted at its region's width whose own content then grows
        (a face settling, a style arriving) with the region unchanged lets a
        ranked column go again, so it still fits its box."""

        self.pages = {**self.pages, "identifier-probe": probe(IDENTIFIER_TABLES, **IDENTIFIER_VALUES)}
        self.open("identifier-probe", 820)
        self.page.evaluate(SETTLE)
        shown = self.page.evaluate(COLUMNS_SHOWN)
        # A style's change, which no script hears as a change to the tree:
        # every sentence now asks for its whole line.
        self.page.evaluate("() => { document.querySelector('main table').style.whiteSpace = 'nowrap'; }")
        # The size a layout reports reaches the page's observers a frame or two later.
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])
        self.assertLess(len(self.page.evaluate(COLUMNS_SHOWN)), len(shown), "no column left")

    @with_script
    def test_the_survey_finds_the_same_twice_on_a_page_that_answers_changes_to_its_tree(self):
        """A page script that answers a change in its tree, as tables.js fits
        a region again when what it holds changes (here the phrase in the
        changed cell is squeezed), hears nothing of what the probes put in
        place and take away to measure, so a second survey judges the page
        the first did. A change from outside the probes is heard and found."""

        self.pages = {**self.pages, "answering-probe": probe(_ANSWERING_TABLE)}
        self.open("answering-probe", PHONE_SCREEN[0])
        self.page.evaluate(_SQUEEZE_WHAT_CHANGES)
        first = self.findings("answering-probe", PHONE_SCREEN[0])
        self.settle()
        self.assertEqual(self.findings("answering-probe", PHONE_SCREEN[0]), first)
        self.page.evaluate("() => document.querySelector('td').append(' ')")
        self.settle()
        self.assertIn("broken words", self.findings("answering-probe", PHONE_SCREEN[0]))

    @with_script
    def test_a_probe_sees_what_it_changes_and_the_page_hears_every_change_but_a_probes(self):
        """Only the page's observers miss a probe's changes: the probe
        measures the copy it put in place, and a change made outside a trip of
        probes is heard."""

        self.pages = {**self.pages, "answering-probe": probe(_ANSWERING_TABLE)}
        self.open("answering-probe", PHONE_SCREEN[0])
        self.page.evaluate(_COUNT_WHAT_IS_HEARD)
        (measured,) = self.read([(_MEASURE_A_COPY, None)])
        self.assertGreater(measured, 0)
        self.assertEqual(self.page.evaluate("() => window.exampleHeard"), 0)
        self.page.evaluate("() => document.querySelector('main').append(document.createElement('span'))")
        self.page.wait_for_function("() => window.exampleHeard > 0")

    def test_a_palette_finding_names_the_token_that_paints_it(self):
        """A fill or an ink one palette loses is named with the token that
        paints it, in that palette, so the fix goes to the token every page
        shares; a color written as a literal is said to be one."""

        self.pages = {
            **self.pages,
            "token-probe": probe('<pre>example.com</pre><pre class="example-literal">example.net</pre>'),
        }
        self.open("token-probe", WIDE)
        # Each token's dark half lost into what it sits on, and the same
        # colors written as literals.
        add_css(
            self.page,
            ":root { --code-bg: light-dark(#161922, var(--bg)); --code-ink: light-dark(#eaeef2, var(--code-bg)); }"
            ".example-literal { background: light-dark(#161922, transparent); color: light-dark(#eaeef2, transparent); }",
        )
        fills = "\n".join(self.lost_fills())
        self.assertRegex(fills, r"dark loses pre \([^\n]*; fill: --code-bg in dark")
        self.assertRegex(fills, r"dark loses pre\.example-literal [^\n]*; fill: no token paints this")
        self.in_scheme("dark")
        text = "\n".join(self.palette()["contrast"])
        self.assertRegex(text, r"pre \d[^\n]*; ink: --code-ink in dark; ground: --code-bg in dark")
        self.assertRegex(text, r"pre\.example-literal \d[^\n]*; ink: no token paints this")

    def test_a_fill_is_judged_on_its_own_box_while_the_page_grows_between_palettes(self):
        """A live page changes between the two readings (a figure that samples
        adds a bar to it), so each box is compared with itself: one that
        shows in both palettes passes, and one a palette loses is found."""

        self.pages = {
            **self.pages,
            "growing-probe": probe('<p><code>example.com</code></p><pre class="example-literal">example.net</pre>'),
        }
        self.open("growing-probe", WIDE)
        add_css(self.page, ".example-literal { background: light-dark(#161922, transparent); }")
        switch = self.in_scheme

        def grown(scheme):
            self.page.evaluate("() => document.querySelector('main').prepend(document.createElement('p'))")
            switch(scheme)

        with mock.patch.object(self, "in_scheme", grown):
            fills = self.lost_fills()
        self.assertEqual([fill for fill in fills if fill.startswith(("light loses code", "dark loses code"))], [])
        self.assertTrue(any(fill.startswith("dark loses pre.example-literal") for fill in fills), fills)
