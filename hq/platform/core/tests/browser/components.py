"""The layout rules held over single components, drawn on probe pages and pages of the crowded estate."""

import base64
import json
import re
from datetime import date, timedelta
from unittest import mock

from django.template.loader import render_to_string

from hq.platform.application import asks
from hq.platform.application.asks import Ask, Standing
from hq.platform.application.ui import ActivityCalendar, CalendarDay, ChartSeries, line_chart
from hq.platform.core.stylesheets import DIRECTORY, stylesheet
from hq.platform.core.test_runner import browser_engine
from hq.platform.core.tests.browser.gate import add_css, tab_key, with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.probes.parts import (
    WRAPPED,
)
from hq.platform.core.tests.browser.probes.settle import (
    FONTS_READY,
    KEPT_SELECTORS,
    PRESS_EVERY_SHOWN,
    SETTLE,
    UNSUPPORTED,
)
from hq.platform.core.tests.browser.probes.shifts import CLS_LIMIT, LAYOUT_SHIFTS
from hq.platform.core.tests.browser.probes.tables import (
    COLUMNS_SHOWN,
    ROW_TOGGLE_TABLES,
    ROW_TOGGLE_VALUES,
    ROW_TOGGLES,
    SORT_BY,
    TABLES_THAT_DO_NOT_FIT,
)
from hq.platform.core.tests.browser.probes.text import (
    OVERLAPPING_TEXT,
)
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import NARROWEST, PHONE_SCREEN, WIDE, WIDTHS, viewport
from hq.platform.core.tests.test_browser_fixtures import PAGES


def authored_selectors(css: str) -> list[tuple[str, tuple[str, ...]]]:
    """Every style rule's selector as the stylesheet writes it, nested rules
    included: the text before each ``{`` that does not open an at-rule. Each
    comes with the condition of every ``@supports`` around it that asks for a
    selector, the guard a selector newer than some engine is written behind."""

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found: list[tuple[str, tuple[str, ...]]] = []
    blocks: list[str] = []
    start, quote = 0, ""
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
                    conditions = (
                        block.removeprefix("@supports").strip() for block in blocks if block.startswith("@supports")
                    )
                    found.append((header, tuple(condition for condition in conditions if "selector(" in condition)))
                blocks.append(header)
            elif char == "}" and blocks:
                blocks.pop()
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
_HELD_BENEATH = "(row, selector) => Boolean(row.nextElementSibling?.querySelector(selector))"


# A table whose one cell holds a phrase, which a probe measures beside a
# one-line twin it puts in place and takes away.
_ANSWERING_TABLE = (
    '{% table_region "Answering" %}<table class="data-table"><thead><tr>{% table_heading "State" "status" %}'
    '</tr></thead><tbody><tr><td><span class="pill">Example running</span></td></tr></tbody></table>'
    "{% endtable_region %}"
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
# width of every action cell shown as a column. A narrow box stacks the table,
# so it has no column to keep; there an open row shows its controls in place.
_QUEUE_COLUMNS = (
    "([queue, act]) => {"
    + DESCRIBE
    + """
  const table = document.querySelector(queue);
  const narrow = narrowBox(table);
  return {
    narrow,
    acts: narrow ? [] : [...table.querySelectorAll(act)].filter((cell) => cell.checkVisibility())
      .map((cell) => Math.round(cell.getBoundingClientRect().width)),
  };
}"""
)

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

# A row deep in the table landed on by a link to it, as the browser lands one:
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


# Each count of a strip: whether its number and its words share one line and
# the count takes no second, whether it is a link, and the top of its line.
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
                    # One Dismiss is shown, at the row's end, at every width:
                    # a row's controls never leave it (tables.css).
                    shown = row.locator(f"{SELECTORS['decision_dismiss']} >> visible=true").count()
                    self.assertEqual(shown, 1)
                    # The row's steps are in its line beneath, drawn by the server.
                    if not row.evaluate(_HELD_BENEATH, SELECTORS["decision_steps"]):
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
        """A row a fold shows offers its toggle in the frame that draws it,
        since the stylesheet decides it from what the server wrote."""

        self.pages = {
            **self.pages,
            "fold-probe": probe(
                '{% table_region "Folding" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Name" %}{% table_heading "Value" %}</tr></thead><tbody>'
                '{% row_group "Example group" 1 folds="example-fold" %}'
                '<tr id="example-cut" data-folded="example-fold"><td>Name</td>'
                "<td>a value far too long to show whole on one line of its column</td>"
                "</tr></tbody></table>{% endtable_region %}"
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
        """A fact's linked value, a target tall a line, wraps without rising
        out of the fact at every width down to the narrowest: under its label
        in a cell, and beside it where the band reads as a list."""

        self.pages = {
            **self.pages,
            "band-fact-probe": probe(
                '<h3 class="eyebrow">Example heading over the band</h3>{% facts layout="band" %}'
                '{% fact "An example label long enough to crowd its value" icon="search" %}'
                '<a href="#example">An example value long enough that it has to wrap in a whole row</a>{% endfact %}'
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
                if width == NARROWEST:
                    # A band too narrow for two cells is a list (surfaces.css).
                    self.assertLess(label[0], first_line[1])
                    self.assertGreater(label[1], first_line[0])
                else:
                    self.assertLessEqual(label[1], first_line[0])

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

    @with_script
    def test_a_rows_ask_says_its_sentence_on_the_line_its_row_opens_to(self):
        """A row's Refresh that fails, pressed in its row or on its open line
        once its column has left, keeps the row's height and says its
        sentence on the row's open line, where a phone with no title to hover
        reads it."""

        # A column of actions that leaves takes its ask to the open line
        # whole, where it is pressed and its note says the sentence itself.
        for width in (820, WIDE):
            with self.subTest(width=width):
                self.open("connections", width)
                row = (
                    self.page.locator(SELECTORS["row_ask"])
                    .first.evaluate_handle("(ask) => ask.closest('tr')")
                    .as_element()
                )
                button = row.query_selector(SELECTORS["ask_button"])
                opened = not button.is_visible()
                if opened:
                    row.query_selector(SELECTORS["row_toggle"]).click()
                    button = row.evaluate_handle(
                        "(row, button) => row.nextElementSibling?.querySelector(button)", SELECTORS["ask_button"]
                    ).as_element()
                    self.assertTrue(
                        button and button.is_visible(), "the row's ask is on neither its row nor its open line"
                    )
                control = button.evaluate_handle("(button, ask) => button.closest(ask)", SELECTORS["ask"])
                tall = row.bounding_box()["height"]
                button.click()
                self.page.wait_for_function("(control) => control.dataset.askState === 'failed'", arg=control)
                self.assertLessEqual(row.bounding_box()["height"], tall + 1)
                if not opened:
                    row.query_selector(SELECTORS["row_toggle"]).click()
                said = row.evaluate(
                    "(row, why) => { const shown = row.nextElementSibling?.querySelector(why);"
                    " return shown?.checkVisibility() ? shown.textContent : null; }",
                    SELECTORS["ask_note"] if opened else SELECTORS["ask_why"],
                )
                self.assertIsNotNone(said, "the open line says nothing of the ask")
                self.assertIn(button.get_attribute("title"), said)

    @with_script
    def test_a_rows_ask_that_succeeds_drops_the_failure_it_last_said(self):
        """A row whose last refresh failed carries the failure as its button's
        title; pressed again and done, the title says what the button is for.
        Its result shows in a region beside it, so the control the press drew
        is still the one on the page."""

        failed = Ask(
            "Refresh",
            "/example/ask/",
            Standing(asks.FAILED, "The example could not be read."),
            title="Read the example again.",
            refresh="[data-example-region]",
        )
        self.pages = {
            **self.pages,
            "ask-probe": probe(
                '{% table_region "Things" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Name" %}{% table_heading "Act" hidden=True %}</tr></thead><tbody>'
                "<tr><td>A</td>"
                '<td>{% include "partials/_ask.html" with ask=ask %}</td></tr></tbody></table>{% endtable_region %}'
                "<div data-example-region><p>An example line.</p></div>"
                '<form id="hq-post" method="post" hidden></form>'
                '{% include "scripts/fragment.html" %}{% include "scripts/ask.html" %}',
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
    def test_an_ask_among_a_rows_actions_keeps_them_on_one_line(self):
        """An ask before another of a row's actions leaves that action on the
        row's first line beside it at every width, and in a wide box what the
        ask says once pressed comes on the line the cell keeps under them, so
        the row grows no taller."""

        self.pages = {
            **self.pages,
            "ask-actions-probe": probe(
                '{% table_region "Things" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Name" %}{% table_heading "Act" hidden=True %}</tr></thead><tbody>'
                '<tr><td>A</td><td class="row-actions">{% include "partials/_ask.html" with ask=ask %}'
                '<a class="btn ghost compact" href="/example/remove/">Remove</a></td></tr></tbody></table>'
                "{% endtable_region %}"
                '<form id="hq-post" method="post" hidden></form>'
                '{% include "scripts/fragment.html" %}{% include "scripts/ask.html" %}',
                ask=Ask("Refresh", "/example/ask/", title="Read the example again.", compact=True),
            ),
            "example/ask": json.dumps(Standing(asks.FAILED, "The example could not be read.").as_json()),
        }
        for width in (PHONE_SCREEN[0], WIDE):
            with self.subTest(width=width):
                self.open("ask-actions-probe", width)
                button = self.page.locator(f"{SELECTORS['row_ask']} [data-ask-button]")
                remove = self.page.get_by_role("link", name="Remove")
                middles = [box["y"] + box["height"] / 2 for box in (button.bounding_box(), remove.bounding_box())]
                self.assertLessEqual(abs(middles[0] - middles[1]), 1.5)
                if width == PHONE_SCREEN[0]:
                    continue
                row = button.evaluate_handle("(button) => button.closest('tr')").as_element()
                tall = row.bounding_box()["height"]
                button.press("Enter")
                self.page.wait_for_selector(f'{SELECTORS["row_ask"]}[data-ask-state="failed"]')
                self.assertTrue(self.page.locator(SELECTORS["ask_note"]).first.is_visible())
                self.assertLessEqual(row.bounding_box()["height"], tall + 1)

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

    @with_script
    def test_a_word_before_a_name_in_a_cell_shares_the_name_s_line(self):
        """The word a cell sets before a name ("on" a machine under a row's
        name, "Created" a record in a cell of free text) stays on the line
        the name opens, never alone above it."""

        from hq.platform.core.templatetags.value_tags import breakable

        host = breakable("example-primary-container-host-with-a-long-name")
        record = breakable("an-example-proxy-host-with-a-deliberately-long-name")
        self.pages = {
            **self.pages,
            "word-before-a-name-probe": probe(
                '<div style="inline-size: 26rem">{% table_region "Changes" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Name" %}{% table_heading "Change" %}</tr></thead><tbody><tr>'
                "<td><strong>example.com</strong><br>"
                f'<span class="muted">on <a href="#" data-entity="Machine">{host}</a></span></td>'
                f'<td class="cell-data">Created {record} from a request</td>'
                "</tr></tbody></table>{% endtable_region %}</div>"
            ),
        }
        # A box that wraps each name, where the column of free text leaves for
        # the row's open line.
        self.open("word-before-a-name-probe", WIDE)
        self.page.locator("main " + SELECTORS["row_toggle"]).click()
        lines = self.page.evaluate(
            """() => [
              // The line under the row's name, and the free text on its open line.
              document.querySelector('main tbody td br').nextElementSibling,
              [...document.querySelectorAll('main .row-detail .token')].map((name) => name.parentElement)
                .find((box) => box.textContent.includes('from a request')),
            ].map((box) => {
              const first = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
                acceptNode: (node) => node.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP,
              }).nextNode();
              const words = document.createRange();
              words.selectNodeContents(first);
              const word = [...words.getClientRects()].find((r) => r.width);
              const name = [...box.querySelector('[data-entity], .token').getClientRects()].find((r) => r.width);
              return [first.data.trim(), Math.abs(word.top - name.top) < word.height / 2];
            })"""
        )
        self.assertEqual(lines, [["on", True], ["Created", True]])

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

    def test_every_selector_parses(self):
        """Every rule the stylesheet writes is one the engine keeps, but for
        another engine's own part (``::-moz-progress-bar`` in WebKit) in a
        rule of its own, and a rule behind an ``@supports selector(…)`` the
        engine says it lacks: an engine drops a whole rule whose list names a
        part it does not know, so such a part shares its rule with nothing,
        and a selector newer than the engine waits behind its guard."""

        authored = authored_selectors(stylesheet())
        self.open(next(iter(PAGES)), WIDTHS[-1])
        kept = {_plain_selector(selector) for selector in self.page.evaluate(KEPT_SELECTORS, f"/{DIRECTORY}/")}
        lacked = set(self.page.evaluate(UNSUPPORTED, sorted({each for _, guards in authored for each in guards})))
        own = "-moz-" if self.browser.browser_type.name == "firefox" else "-webkit-"

        def anothers_part_alone(selector):
            parts = set(re.findall(r"::(-(?:moz|webkit)-)", selector))
            return bool(parts) and own not in parts and not _top_level_comma(selector)

        dropped = [
            selector
            for selector, guards in authored
            if _plain_selector(selector) not in kept and not lacked.intersection(guards)
        ]
        self.assertEqual([selector for selector in dropped if not anothers_part_alone(selector)], [])

    def test_a_share_fills_its_bar_from_edge_to_edge(self):
        """A share's fill is as tall as its bar, in every engine (`.bar-track`
        in pages.css)."""

        self.open(next(iter(PAGES)), WIDE)
        self.page.evaluate(_A_SHARE_ROW)
        # The page's own shot, which drops the style WebKit adds to take one.
        shot = self.page.screenshot(clip=self.page.locator(SELECTORS["share_bar"]).first.bounding_box())
        fill = self.page.evaluate(_FILL_ROWS, base64.b64encode(shot).decode())
        self.assertGreater(fill["rows"], 2)
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
        something in it, a part of a list it folds, a run of parts with a rest
        past the ones that size its column, or in a narrow box a value longer
        than a line of its column, as the server marks it, which a wide box
        holds whole, unclamped (cuts.css). A dash in a dropped column says
        nothing, screen-reader text is clipped to a pixel by design and a
        decoration is hidden from assistive technology: none is content a row
        withholds. Nothing is measured: the server and the region decide.
        """

        self.pages = {**self.pages, "row-toggle-probe": probe(ROW_TOGGLE_TABLES, **ROW_TOGGLE_VALUES)}
        shown_at = {
            PHONE_SCREEN[0]: {"dropped", "cut", "folded", "cut-in-a-kept-gutter", "line-cut"},
            1280: {"folded", "line-cut"},
        }
        for width, expected in shown_at.items():
            with self.subTest(width=width):
                self.open("row-toggle-probe", width)
                self.page.evaluate(SETTLE)
                rows = self.page.evaluate(ROW_TOGGLES)
                self.assertEqual({(row["toggles"], row["beside_name"]) for row in rows}, {(1, True)})
                self.assertEqual({row["probe"] for row in rows if row["shown"]}, expected)

    def test_a_menu_is_judged_beside_its_button_and_one_wider_than_the_screen_still_fails(self):
        """A menu's panel is judged shown as a reader opens it, its button in
        view: one that fits beside its button passes, wherever on a long page
        the button lies, and one wider than the screen fails, named."""

        menu = (
            '<p class="example-tall"></p><div data-menu><button type="button" popovertarget="example-panel">More'
            '</button><div id="example-panel" popover><a href="/example/">An example action</a></div></div>'
            '<p class="example-tall"></p>'
        )
        self.pages = {**self.pages, "menu-probe": probe(menu)}
        self.open("menu-probe", 402)
        add_css(self.page, ".example-tall { block-size: 4000px; } #example-panel { inline-size: 340px; }")
        self.every_anchored_popover_keeps_the_layout_rules("menu-probe")
        add_css(self.page, "#example-panel { inline-size: 480px; }")
        with self.assertRaises(AssertionError) as failed:
            self.every_anchored_popover_keeps_the_layout_rules("menu-probe")
        self.assertIn("with #example-panel shown", str(failed.exception))

    @with_script
    def test_opening_every_row_a_reader_can_open_moves_nothing_they_did_not_ask_for(self):
        """A fold and a row's toggle pressed as a reader presses them push
        what is below the rows they open, which the browser counts as the
        reader's own doing; nothing else moves, at any width, so the page
        stays under `CLS_LIMIT`."""

        read = [SELECTORS["vendored"], CLS_LIMIT]
        folds = "main " + SELECTORS["row_toggle"] + "[data-fold-toggle]"
        rows = "main " + SELECTORS["row_toggle"] + ":not([data-fold-toggle])"
        for name in ("action-items", "containers"):
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
    def test_the_face_arriving_late_is_the_gates_move_under_a_fixed_clock(self):
        """The gates draw text in Inter once it arrives (`serving.in_the_face`),
        where a reader's browser draws it from the first paint or never, so the
        move its arrival makes is left out of the score however late it lands,
        under the fixed clock `dev:look` reads every page by too."""

        self.pages = {
            **self.pages,
            "late-face-probe": probe(
                "<h1>"
                + "An example heading that wraps in either face " * 3
                + "</h1>"
                + '<div class="card"><p>'
                + "An example sentence below it. " * 30
                + "</p></div>"
            ),
        }
        if browser_engine() != "chromium":
            self.skipTest("only Chromium keeps a record of layout shifts")
        held = []

        def hold(route):
            # Playwright marks its handler, which a list's bound method refuses.
            held.append(route)

        self.page.route("**/*.woff2", hold)
        self.page.clock.set_fixed_time(1_800_000_000)
        self.page.set_viewport_size(viewport(NARROWEST))
        self.page.goto(f"{ORIGIN}/late-face-probe/", wait_until="domcontentloaded")
        # Drawn in the next face for well past the margin the face is given.
        self.page.wait_for_function("() => document.timeline.currentTime > 300")
        if not held:
            self.skipTest("Inter is withheld (HQ_BROWSER_FACE=fallback)")
        for route in held:
            route.fallback()
        self.page.evaluate(FONTS_READY)
        self.settle()
        self.assertEqual(self.page.evaluate(LAYOUT_SHIFTS, [SELECTORS["vendored"], 0]), [])

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
    def test_a_table_with_rows_open_is_fitted_as_it_stands_with_them_closed(self):
        """A table without headings, its rows opened by their toggles or by
        any other way, keeps the columns it had closed when content inside
        its region changes: it stays in its narrow box, and no action cell
        widens around what an open row shows whole."""

        toggles = SELECTORS["row_toggle"] + ":not([data-fold-toggle])"
        for width in (PHONE_SCREEN[0], 820):
            for how in ("toggle", "class"):
                with self.subTest(width=width, opened_by=how):
                    self.open("action-items", width)
                    self.page.locator(SELECTORS["decision_family"]).evaluate_all(PRESS_EVERY_SHOWN)
                    self.page.evaluate(SETTLE)
                    columns = [SELECTORS["queue_table"], SELECTORS["queue_act"]]
                    closed = self.page.evaluate(_QUEUE_COLUMNS, columns)
                    # In a narrow box the buttons stand beneath the words and their column leaves.
                    self.assertTrue(closed["acts"] or closed["narrow"])
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
                self.open("containers", width)
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

    @with_script
    def test_what_the_dashboard_says_of_its_readings_covers_none_of_them(self):
        """Opened while a reading is due, the dashboard asks for it and says it
        is waiting for the controller. That line stands where a trail would,
        and the dashboard's readings stand there instead: it is said on a line
        no other text is drawn on, whichever row the readings take."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("dashboard", width)
                self.page.locator(SELECTORS["page_note"]).wait_for(state="visible")
                self.assertEqual(self.page.evaluate(OVERLAPPING_TEXT, SELECTORS["floating"]), [])

    @with_script
    def test_a_name_a_cell_cuts_is_drawn_whole_on_its_line_where_its_cell_has_room(self):
        """A one-token name in a box of its own (a bare name, a cell's line,
        a link, a name an icon opens), in a cell with room to spare, draws
        every part on its one line uncut, in a box at least as wide as what it
        draws, read in every engine at each density a screen has."""

        from hq.platform.core.templatetags.value_tags import breakable, cell_line

        shapes = ("example-connect", "example2-proxy", "example2-proxy-web", "example-id", "example-hq", "example-a1")
        names = [f"/opt/apps/{name}" for name in shapes]
        icon = render_to_string("partials/_icon.html", {"name": "deploy"}).strip()
        cells = [
            *(breakable(name) for name in names),
            *(cell_line(name) for name in names),
            *(f'<a data-entity href="/example/">{breakable(name)}</a>' for name in names),
            *(f"{icon} {breakable(name)}" for name in [*names, "2024.12.1-ubu2404-ls48"]),
        ]
        rows = "".join(f"<tr><td>{cell}</td><td>Example</td></tr>" for cell in cells)
        self.pages = {
            **self.pages,
            "name-room-probe": probe(
                '{% table_region "Directories" %}<table class="data-table"><thead><tr>'
                '{% table_heading "Directory" "identifier" %}{% table_heading "State" "status" %}</tr>'
                f"</thead><tbody>{rows}</tbody></table>{{% endtable_region %}}"
            ),
        }
        short = """([boxes, whole]) => {
          const found = [];
          const range = document.createRange();
          let read = 0;
          for (const box of document.querySelectorAll(boxes)) {
            if (!box.checkVisibility()) continue;
            read += 1;
            const top = box.getClientRects()[0].top;
            const parts = [...box.querySelectorAll(whole)];
            const pad = getComputedStyle(box);
            range.selectNodeContents(box);
            const inside = box.getBoundingClientRect().width - parseFloat(pad.paddingLeft) - parseFloat(pad.paddingRight);
            const spare = inside - range.getBoundingClientRect().width;
            // A part is text of its box, set within the box's line: on the
            // next line it stands a line lower, never a fraction of one.
            const off = parts.filter((part) => {
              const drawn = part.getBoundingClientRect();
              return drawn.top > top + drawn.height / 2 || part.scrollWidth > part.clientWidth;
            });
            if (off.length || spare < 0)
              found.push(`${box.textContent} spares ${spare.toFixed(2)}px, ${off.length} of its parts off its line or cut`);
          }
          return {read, found};
        }"""
        for scale in (1, 1.5, 2):
            with self.subTest(scale=scale):
                self.start(java_script_enabled=True, device_scale_factor=scale)
                self.open("name-room-probe", WIDE)
                self.page.evaluate(SETTLE)
                drawn = self.page.evaluate(short, [SELECTORS["name_box"], SELECTORS["part"]])
                self.assertGreater(drawn["read"], 0)
                self.assertEqual(drawn["found"], [])

    @with_script
    def test_a_table_whose_content_widens_after_it_is_drawn_still_fits_and_moves_no_column(self):
        """A table whose own content grows once it is drawn (a face settling,
        a style arriving) with its region unchanged keeps the columns it drew
        and still fits its box: a column leaves by the region's width alone,
        and what grows is cut in its cell."""

        from hq.platform.core.tests.browser.catalog import page

        name = "table/identifiers"
        self.pages = {**self.pages, name: page(name)}
        self.open(name, 820)
        self.page.evaluate(SETTLE)
        shown = self.page.evaluate(COLUMNS_SHOWN)
        # A style's change, which no script hears as a change to the tree:
        # every sentence now asks for its whole line.
        self.page.evaluate("() => { document.querySelector('main table').style.whiteSpace = 'nowrap'; }")
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"])["found"], [])
        self.assertEqual(self.page.evaluate(COLUMNS_SHOWN), shown)

    @with_script
    def test_the_survey_finds_the_same_twice_on_a_page_that_answers_changes_to_its_tree(self):
        """A page script that answers a change in its tree (here the phrase in
        the changed cell is squeezed), hears nothing of what the probes put in
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
            return switch(scheme)

        with mock.patch.object(self, "in_scheme", grown):
            fills = self.lost_fills()
        self.assertEqual([fill for fill in fills if fill.startswith(("light loses code", "dark loses code"))], [])
        self.assertTrue(any(fill.startswith("dark loses pre.example-literal") for fill in fills), fills)
