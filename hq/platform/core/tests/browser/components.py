"""The layout rules held over single components, drawn on probe pages and pages of the stress estate."""

import re
from decimal import Decimal

from hq.platform.application.money import money
from hq.platform.core.stylesheets import DIRECTORY, stylesheet
from hq.platform.core.tests.browser.gate import add_css, with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.boxes import _MAP, _OVERRULED_ALIGNMENT, _SMALL_TARGETS
from hq.platform.core.tests.browser.probes.palette import _BUTTON_EDGES
from hq.platform.core.tests.browser.probes.parts import _READINGS, _SPILLED_LABELS, reading_problems
from hq.platform.core.tests.browser.probes.settle import _KEPT_SELECTORS, _PRESS_EVERY_SHOWN, _SETTLED
from hq.platform.core.tests.browser.probes.tables import (
    _BLANK_TABLE,
    _CELLS,
    _COLUMNS_SHOWN,
    _PHONE_TABLE,
    _PRESS_FIRST_ROW_TOGGLE,
    _ROW_TOGGLE_TABLES,
    _ROW_TOGGLES,
    _SORT_BY,
    _TABLES_THAT_DO_NOT_FIT,
)
from hq.platform.core.tests.browser.probes.text import _BROKEN_WORDS, _BROKEN_WORDS_IN_TEXT, _OVERLAPPING_TEXT
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.stress import ALL_PAGES
from hq.platform.core.tests.browser.survey import _IDENTIFIER
from hq.platform.core.tests.browser.widths import NARROWEST, PHONE_SCREEN, TARGET_MIN, WIDE, WIDTHS
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


def _plain_selector(selector: str) -> str:
    """A selector as the browser writes it back: no whitespace, double quotes,
    no ``*`` before what already names an element, and every attribute value
    quoted."""

    plain = re.sub(r"\s+", "", selector).replace("'", '"')
    plain = re.sub(r"\*(?=[:.\[#])", "", plain)
    plain = plain.replace("(odd)", "(2n+1)").replace("(even)", "(2n)")
    return re.sub(r'(\[[^\]=]+?[~|^$*]?=)([^"\]]+)\]', r'\1"\2"]', plain)


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
                    # Held by its node: opening the row moves it to the line beneath.
                    steps = row.locator(SELECTORS["decision_steps"]).element_handles()
                    if not steps:
                        continue
                    self.assertFalse(steps[0].is_visible())
                    row.locator(SELECTORS["row_toggle"]).click()
                    self.assertTrue(steps[0].is_visible())
                    row.locator(SELECTORS["row_toggle"]).click()
                    self.assertFalse(steps[0].is_visible())
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

                self.assertNotIn(True, shown())
                toggle.click()
                self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
                self.assertNotIn(False, shown())
                toggle.click()
                self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
                self.assertNotIn(True, shown())

    @with_script
    def test_an_opened_row_keeps_its_words_in_their_cells(self):
        """Opening a row lifts every cut in it, and what was cut breaks inside
        its column rather than running under the next one."""

        for name in ALL_PAGES:
            for width in (PHONE_SCREEN[0], 820):
                with self.subTest(page=name, width=width):
                    self.open(name, width)
                    # A family first, so the rows it folds show their own toggles.
                    self.page.locator(SELECTORS["decision_family"]).evaluate_all(_PRESS_EVERY_SHOWN)
                    self.page.locator(SELECTORS["row_toggle"] + ":not([data-fold-toggle])").evaluate_all(
                        _PRESS_EVERY_SHOWN
                    )
                    self.page.evaluate(_SETTLED)
                    self.assertEqual(self.page.evaluate(_CELLS), [])
                    self.assertEqual(self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

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
        found = self.page.evaluate(_SPILLED_LABELS, "main :is(.example-star, .example-word)")
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
        found = [line for line in self.page.evaluate(_CELLS) if "example-" in line]
        self.assertEqual([line.split(" runs ")[0] for line in found], ["div.example-box"])

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
        found = [line for line in self.page.evaluate(_BROKEN_WORDS) if "abcdef" in line or "example" in line]
        self.assertEqual(len(found), 1)
        self.assertIn("example", found[0])

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

        found = [line for line in self.page.evaluate(_BROKEN_WORDS_IN_TEXT, _IDENTIFIER) if "example-" in line]
        self.assertEqual(len(found), 1, found)
        self.assertIn("example-loose", found[0])

    def test_a_toolbars_menus_wear_a_buttons_edge_and_its_search_a_fields(self):
        """A list's filter menu, toggle and sort are pressed, so in either
        palette none is edged as the search box beside them is, and a control
        given the field's edge is found."""

        self.open("projects", WIDE)
        self.assertGreater(self.page.locator(SELECTORS["table_filter"]).count(), 0)
        for palette in ("light", "dark"):
            with self.subTest(palette=palette):
                self.page.emulate_media(color_scheme=palette)
                self.assertEqual(self.page.evaluate(_BUTTON_EDGES, SELECTORS["pressed"]), [])
        self.page.evaluate("""() => {
          const button = document.createElement('button');
          button.className = 'example-edged';
          button.textContent = 'Example';
          button.style.setProperty('border-color', 'var(--field-line)');
          document.querySelector('main').append(button);
        }""")
        self.assertEqual(
            self.page.evaluate(_BUTTON_EDGES, SELECTORS["pressed"]), ["button.example-edged wears a field's edge"]
        )

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
        found = self.page.evaluate(_SMALL_TARGETS, ["main :is(.example-hidden, .example-shown)", TARGET_MIN])
        self.assertEqual({finding.split(" in ")[0] for finding in found}, {"a.example-shown"})

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
          const menu = document.createElement('details');
          menu.dataset.menu = '';
          menu.open = true;
          const summary = document.createElement('summary');
          summary.textContent = 'menu';
          const panel = document.createElement('div');
          panel.append(line('example covering', '300px', '40px'), line('example first', '400px', '40px'), line('example second', '400px', '40px'));
          menu.append(summary, panel);
          const main = document.querySelector('main');
          main.append(line('example covered', '300px', '40px'), menu, line('example clashed', '500px', '40px'));
          main.append(line('example under', '500px', '40px'));
        }""")
        found = self.page.evaluate(_OVERLAPPING_TEXT, SELECTORS["floating"])
        pairs = {tuple(sorted(part.split('"')[1] for part in finding.split(" over "))) for finding in found}
        self.assertNotIn(("example covered", "example covering"), pairs)
        self.assertIn(("example clashed", "example under"), pairs)
        self.assertIn(("example first", "example second"), pairs)

    def test_every_selector_parses(self):
        authored = authored_selectors(stylesheet())
        self.open(next(iter(PAGES)), WIDTHS[-1])
        kept = {_plain_selector(selector) for selector in self.page.evaluate(_KEPT_SELECTORS, f"/{DIRECTORY}/")}

        self.assertEqual([selector for selector in authored if _plain_selector(selector) not in kept], [])

    def test_the_dashboard_pairs_its_cards_on_a_wide_screen(self):
        self.open("dashboard", 1280)
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assertGreater(max(len(row) for row in self.card_rows(key)), 1)
                self.assert_rows_filled(key)

    def test_the_dashboard_stacks_its_cards_on_a_phone(self):
        for width in (NARROWEST, PHONE_SCREEN[0]):
            self.open("dashboard", width)
            for key in ("highlights", "patterns"):
                with self.subTest(cards=key, width=width):
                    rows = self.card_rows(key)
                    self.assertGreater(len(rows), 1)
                    self.assertEqual({len(row) for row in rows}, {1})
                    self.assertEqual({round(row[0]["left"]) for row in rows}, {round(rows[0][0]["left"])})

    def test_a_dashboard_without_contributors_draws_no_empty_pairs(self):
        self.open("dashboard-bare", 1280)
        # Without extensions, the cards that remain still fill their rows.
        for key in ("highlights", "patterns"):
            with self.subTest(cards=key):
                self.assert_rows_filled(key)

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

        self.pages = {**self.pages, "row-toggle-probe": probe(_ROW_TOGGLE_TABLES)}
        shown_at = {
            PHONE_SCREEN[0]: {"dropped", "cut", "folded", "cut-in-a-kept-gutter"},
            1280: {"folded", "cut-in-a-kept-gutter"},
        }
        for width, expected in shown_at.items():
            with self.subTest(width=width):
                self.open("row-toggle-probe", width)
                self.page.evaluate(_SETTLED)
                rows = self.page.evaluate(_ROW_TOGGLES)
                self.assertEqual({(row["toggles"], row["beside_name"]) for row in rows}, {(1, True)})
                self.assertEqual({row["probe"] for row in rows if row["shown"]}, expected)

    @with_script
    def test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing(self):
        """A table of four columns or more keeps the column that names each row
        and the ones marked key, and fits the screen; it does not stack into
        cards and is not swiped through. What left is listed when a row is
        opened, beneath that row, and opening it changes no column's width and
        no other row's height: the reader's place in the table holds.
        """

        for name in ("connections", "services", "containers", "projects"):
            with self.subTest(page=name):
                self.open(name, PHONE_SCREEN[0])
                closed = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertGreaterEqual(closed["headings"], 4)
                self.assertLess(len(closed["columns"]), closed["headings"])
                self.assertLessEqual(closed["sideways"], 1)
                self.assertGreater(closed["toggles"], 0)

                pressed = self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertTrue(opened["details"])
                self.assertEqual(opened["columns"], closed["columns"])
                self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))
                self.assertLessEqual(opened["sideways"], 1)

                self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
                again = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
                self.assertEqual(again["details"], [])
                self.assertFalse(again["held"])
                self.assertEqual(again["columns"], closed["columns"])
                self.assertEqual(again["rows"], closed["rows"])

    @with_script
    def test_a_fragment_that_is_not_percent_encoding_breaks_no_table(self):
        """`#%` is a legal fragment and no encoding of a row's id: it names no
        row, and everything the table script sets up after landing still runs."""

        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        self.page.goto(f"{ORIGIN}/projects/#%", wait_until="load")
        ready = self.page.eval_on_selector_all(
            SELECTORS["selectable_table"], "(tables) => tables.map((table) => table.dataset.selectionReady)"
        )
        self.assertEqual(errors, [])
        self.assertTrue(ready)
        self.assertEqual(set(ready), {"true"})

    @with_script
    def test_a_wide_table_on_a_desktop_keeps_every_column_and_holds_them_when_a_row_opens(self):
        self.open("connections", 1440)
        closed = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
        self.assertEqual(len(closed["columns"]), closed["headings"])

        if closed["toggles"]:
            pressed = self.page.evaluate(_PRESS_FIRST_ROW_TOGGLE, SELECTORS["table_scroll"])
            opened = self.page.evaluate(_PHONE_TABLE, SELECTORS["table_scroll"])
            self.assertEqual(opened["columns"], closed["columns"])
            self.assertEqual(others(opened["rows"], pressed), others(closed["rows"], pressed))

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
        up = self.page.evaluate(_SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(up["sort"], "ascending")
        self.assertEqual(up["values"], [sorted(group, key=str.lower) for group in came])
        down = self.page.evaluate(_SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(down["sort"], "descending")
        self.assertEqual(down["values"], [sorted(group, key=str.lower, reverse=True) for group in came])
        back = self.page.evaluate(_SORT_BY, [SELECTORS["table_scroll"], 0])
        self.assertEqual(back["sort"], "none")
        self.assertEqual(back["values"], came)

    def test_the_dashboards_readings_are_one_shape(self):
        """Each is a mark, a lead and its lines, and they are the same shape:
        the mark beside the lead and nothing set under it, every lead one size,
        every second line starting the same distance down, and no line cut
        short. A caption that fell into the mark's own narrow column read as
        "Via h" and "Has", and nothing else in the gate would have said so.
        """

        for width in (PHONE_SCREEN[0], 820, 1280, 1440):
            with self.subTest(width=width):
                self.open("dashboard", width)
                readings = self.page.locator(SELECTORS["readings"]).evaluate_all(_READINGS)
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
                self.assertEqual(self.page.evaluate(_OVERLAPPING_TEXT, SELECTORS["floating"]), [])

    def test_the_map_scrolls_with_the_page_and_draws_no_empty_lane(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("topology", width)
                found = self.page.evaluate(_MAP, [SELECTORS["map"], SELECTORS["map_lane"], SELECTORS["map_node"]])
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
        add_css(self.page, "@media (prefers-color-scheme: dark) { body { background: rgb(1, 2, 3) } }")
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
        self.pages = {**self.pages, "blank-column-probe": probe(_BLANK_TABLE)}
        for width in WIDTHS:
            with self.subTest(width=width):
                self.open("blank-column-probe", width)
                self.page.evaluate(_SETTLED)
                shown = self.page.evaluate(_COLUMNS_SHOWN)
                self.assertNotIn("Nothing", shown)
                self.assertIn("Title", shown)
                self.assertIn("Runs", shown)
                self.assertEqual(self.page.evaluate(_BROKEN_WORDS), [])

    def test_an_identifier_sits_where_its_end_of_the_connection_aligns_it(self):
        """The address on each end of the connection is placed with the rest
        of its end: centered with it when the ends stack on a phone, at the
        end on HQ's side when they face each other."""

        for width, sides in ((PHONE_SCREEN[0], {"center"}), (WIDE, {"start", "end"})):
            with self.subTest(width=width):
                self.open("connection", width)
                ends = self.page.locator(SELECTORS["connection_end"]).evaluate_all(
                    "(ends) => ends.map((end) => getComputedStyle(end).justifyItems)"
                )
                self.assertEqual(set(ends), sides)
                self.assertEqual(self.page.evaluate(_OVERRULED_ALIGNMENT, SELECTORS["chip"]), [])
