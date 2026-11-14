"""The layout rules held over the stress estate, which the sweeps run."""

import json
import re
from itertools import islice, takewhile
from time import perf_counter
from typing import override

lazy from playwright.sync_api import TimeoutError as LoadTimeout

from hq.platform.core.tests.browser.gate import RULE_COSTS, BrowserGate, answered_as
from hq.platform.core.tests.browser.probes.axe import contrast_read
from hq.platform.core.tests.browser.probes.boxes import (
    ANY_SCROLLER,
    CHROME_INSETS,
    INTO_A_NEIGHBOR,
    NARROW_FRAMES,
    OVERLAPS,
    SIDEWAYS,
)
from hq.platform.core.tests.browser.probes.palette import PAINT
from hq.platform.core.tests.browser.probes.parts import EVIDENCE_READ, SUMMARIES_READ
from hq.platform.core.tests.browser.probes.settle import FINISH_TRANSITIONS, SETTLE, STYLESHEETS
from hq.platform.core.tests.browser.probes.tables import (
    CELLS,
    CUT_BESIDE_ROOM,
    PAST_THE_GUTTER,
    TABLES,
    TABLES_THAT_DO_NOT_FIT,
)
from hq.platform.core.tests.browser.probes.text import (
    BROKEN_WORDS,
    CUT_DECISIONS,
    PAST_THE_CLAMP,
)
from hq.platform.core.tests.browser.reading import reads, taken
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.stress import stress_estate
from hq.platform.core.tests.browser.survey import AS_FINDINGS, CHECKS
from hq.platform.core.tests.browser.widths import SPARSE_WIDTHS, WIDTHS
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES
from hq.platform.core.tests.test_browser_fixtures import PAGES

# A key that changes nothing, pressed before a batch a gate opens, so the
# browser takes what the batch moves as the reader's own doing (`as_a_reader`).
_NO_OP_KEY = "Shift"

# What runs past the page's edge, wherever a box that scrolls does not hold it.
_ESCAPES_READ = (SIDEWAYS, ANY_SCROLLER)

# A page's note on its readings, saying nothing and then the longest line it
# says (a failure that names what it could not read), against the band
# between the bar and the page: where it stands, how far its end is from the
# end of the person's control, and where each part of the page is with it
# silent, saying that, and gone; and whether each sentence its slot keeps room
# for (its struts) is drawn whole. The note is left saying what it said.
_PAGE_NOTE = """([noteSelector, lineSelector, contentSelector]) => [...document.querySelectorAll(noteSelector)].map((note) => {
  const content = document.querySelector(contentSelector);
  const line = note.closest(lineSelector);
  // Every part of the page but the note, where it lands.
  const parts = () => [...line.children, ...content.querySelectorAll(':scope > *, :scope > * > *')]
    .filter((part) => part !== note && part !== line && part.getClientRects().length)
    .map((part) => { const r = part.getBoundingClientRect(); return [r.top, r.left, r.width, r.height].map(Math.round).join(' '); });
  const said = note.textContent;
  const cut = [...note.parentElement.children].filter((strut) => strut.dataset.strut).map((strut) => {
    note.textContent = strut.dataset.strut;
    return note.scrollWidth > note.clientWidth + 0.5 ? strut.dataset.strut : null;
  }).filter(Boolean);
  note.textContent = '';
  const silent = parts();
  note.textContent = 'Cannot refresh: ' + Array.from({length: 24}, (_, at) => `example reading ${at}`).join(', ') + '.';
  const saying = parts();
  const r = note.getBoundingClientRect();
  const box = line.getBoundingClientRect();
  const placed = {top: r.top - box.top, bottom: box.bottom - r.bottom, start: r.left - box.left, end: box.right - r.right,
    lines: note.scrollHeight > note.clientHeight + 0.5};
  const [slot, sibling] = [note.parentElement, note.nextSibling];
  note.remove();
  const gone = parts();
  slot.insertBefore(note, sibling);
  note.textContent = said;
  return {silent, saying, gone, placed, cut};
})"""


class LayoutBrowserTests(BrowserGate):
    """The layout rules over the stress estate.

    Each page is opened once per width and per configuration (script off,
    script on), and every rule that holds there is read off that one load,
    each in a subtest named for the rule, the page and the width. The rules
    that change the page (open its disclosures, dispatch a resize) run after
    the ones that only read it.

    The tests are the classes below, a sweep or a share of one each: Django
    hands a parallel worker a whole class, so they run side by side, and a
    run lasts as long as its longest class.
    """

    lists: tuple[str, ...]

    @classmethod
    @override
    def render(cls):
        pages, cls.lists = stress_estate()
        return pages

    def loaded(self, name, width, **where):
        """Open ``name`` at ``width``; one that does not load fails once, not once per rule."""

        # subTest records a failure and carries on past the block, with nothing opened.
        opened: list[bool] = []
        with self.subTest(page=name, width=width, **where):
            self.open(name, width)
            opened.append(True)
        return bool(opened)

    def hold(self, name, width, rules, **where):
        """Hold each rule on the page as it stands, one subtest each, in
        order. The probes of a run of rules that only read are read in one
        round trip before the first of them is judged; a probe that threw
        fails its own rule only. Each subtest's time, its probes' time in the
        page with it, goes to the run's ledger."""

        read: dict[int, list] = {}
        for at, rule in enumerate(rules):
            if hasattr(rule, "probes") and at not in read:
                read |= self.read_ahead(rules, at)
            began = perf_counter() - sum(seconds for _returned, _value, seconds in read.get(at, []))
            try:
                with self.subTest(rule=rule.__name__, page=name, width=width, **where):
                    if at in read:
                        rule(name, *map(taken, read[at]))
                    else:
                        rule(name)
            finally:
                self.spent(RULE_COSTS, rule.__name__, perf_counter() - began, f"{name} at {width}px")

    def read_ahead(self, rules, at):
        """The timed outcomes of each rule's probes, by its place in
        ``rules``, for the run of reading rules that starts at ``at``."""

        run = list(takewhile(lambda rule: hasattr(rule, "probes"), rules[at:]))
        outcomes = iter(self.timed_outcomes([probe for rule in run for probe in rule.probes]))
        return {at + offset: list(islice(outcomes, len(rule.probes))) for offset, rule in enumerate(run)}

    # Without script: the page as the server drew it. Layout must hold before
    # progressive enhancement.

    def unscripted_reads(self, name, width):
        """The rules that only read the page: on the sparse estate's pages at
        every SPARSE_WIDTHS width, and on every page at every WIDTHS width."""

        rules = []
        if name in PAGES and width in SPARSE_WIDTHS:
            rules += [
                self.every_stylesheet_loads,
                self.nothing_escapes_the_page_sideways,
                self.tables_scroll_inside_their_own_container,
                self.nothing_overlaps,
                self.layout_audit_structural_rules,
            ]
        if width in WIDTHS:
            reads = [*self.overflow_reads(name), self.a_heads_note_costs_its_head_no_room]
            rules += [rule for rule in reads if rule not in rules]
        return rules

    @classmethod
    def unscripted_rules(cls):
        """Each rule the sweep without script holds, by name, with the widths
        it holds it at on some page, read off the lists the sweep runs."""

        lists = cls.__new__(cls)
        held: dict[str, set[int]] = {}
        for name in (*PAGES, *DENSE_PAGES):
            for width in {*WIDTHS, *SPARSE_WIDTHS}:
                for rule in (*lists.unscripted_reads(name, width), *lists.unscripted_openings(name, width)):
                    held.setdefault(rule.__name__, set()).add(width)
        return {rule: sorted(widths) for rule, widths in held.items()}

    def overflow_reads(self, name):
        rules = [
            self.tables_scroll_inside_their_own_container,
            self.no_block_breaks_a_paragraph,
            self.words_stay_inside_the_box_that_holds_them,
            self.only_a_table_scrolls_sideways,
            self.nothing_runs_out_of_its_table_cell,
            self.a_table_frame_spans_its_box,
            self.no_column_is_narrower_than_its_words,
            self.a_checks_evidence_follows_its_statement,
            self.nothing_a_summary_names_is_hidden,
            self.a_clamped_box_shows_only_its_lines,
            self.what_an_item_means_is_never_cut,
            self.code_and_cards_keep_inside_the_gutter,
            self.nothing_is_drawn_into_its_neighbor,
            self.the_chrome_is_as_tall_as_its_insets_say,
        ]
        if name in DENSE_PAGES:
            rules.append(self.dense_pages_keep_to_their_boxes)
        return rules

    def unscripted_openings(self, name, width):
        """The rules that open the page's disclosures, after every read."""

        rules = [self.open_every_disclosure]
        if name in PAGES and width in SPARSE_WIDTHS:
            rules.append(self.nothing_escapes_the_page_sideways_with_every_disclosure_open)
        if width in WIDTHS:
            rules += [
                self.only_a_table_scrolls_sideways_with_every_disclosure_open,
                self.a_checks_evidence_follows_its_statement,
                self.nothing_a_summary_names_is_hidden,
                self.a_fill_shows_in_both_palettes,
            ]
        return rules

    @reads(STYLESHEETS)
    def every_stylesheet_loads(self, _name, sheets):
        self.assertGreater(sheets["links"], 0)
        self.assertEqual(sheets["loaded"], sheets["links"])
        self.assertEqual(sheets["empty"], [])

    @reads(_ESCAPES_READ)
    def nothing_escapes_the_page_sideways(self, _name, found):
        self.assertEqual(found, [])

    @reads(CHROME_INSETS)
    def the_chrome_is_as_tall_as_its_insets_say(self, _name, found):
        self.assertEqual(found, [])

    def open_every_disclosure(self, _name):
        """Every disclosure and popover open, for the rules that follow."""

        # A popover placed against an anchor in the page (a filter's panel) can
        # escape it, so it opens with the disclosures; a centered one, a
        # modal, covers the page by design and is held by the dialog checks.
        # An auto popover closes the others as it opens, so each is held
        # manual to stay shown beside the rest.
        self.as_a_reader("""() => {
          document.querySelectorAll('details').forEach((d) => { d.open = true; });
          document.querySelectorAll('[popover]:not(:popover-open)').forEach((p) => {
            if (['auto', 'none', 'normal', undefined].includes(getComputedStyle(p).positionAnchor)) return;
            p.popover = 'manual';
            p.showPopover();
          });
        }""")

    def as_a_reader(self, script, arg=None):
        """``script`` run as a reader's press: a script's click is no input
        to the browser, so Chromium would score what the batch pushes down
        as the page moving by itself. A trusted key first, which changes
        nothing, lets its rule for the half second after an input (Layout
        Instability, `hadRecentInput`) excuse the batch, as it excuses each
        of a reader's taps; a shift that lands later is still the page's."""

        self.page.keyboard.press(_NO_OP_KEY)
        return self.page.evaluate(script, arg)

    @reads(_ESCAPES_READ)
    def nothing_escapes_the_page_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    def a_heads_note_costs_its_head_no_room(self, name):
        """A page's note on its readings ends the head's first line, inside
        it and on one line however long what it says: the page is laid out as
        it is with no note at all, saying something moves nothing, and each
        sentence HQ writes for it word for word is drawn whole."""

        notes = self.page.evaluate(
            _PAGE_NOTE, [SELECTORS["page_note"], SELECTORS["page_trail_row"], SELECTORS["content"]]
        )
        if 'form="page-visit"' in self.pages[name]:
            self.assertGreater(len(notes), 0, "the probe measured nothing")
        for note in notes:
            self.assertEqual(note["silent"], note["gone"])
            self.assertEqual(note["saying"], note["gone"])
            placed = note["placed"]
            self.assertGreaterEqual(placed["top"], -0.5)
            self.assertGreaterEqual(placed["bottom"], -0.5)
            self.assertGreaterEqual(placed["start"], -0.5)
            self.assertGreaterEqual(placed["end"], -0.5)
            self.assertFalse(placed["lines"])
            self.assertEqual(note["cut"], [])

    def no_block_breaks_a_paragraph(self, _name):
        # A block written inside a <p> closes it early and leaves an empty <p>
        # where its end tag was: the rest of the line falls out of it.
        self.assertEqual(self.page.locator(SELECTORS["broken_paragraph"]).count(), 0)

    @reads(CHECKS["control labels"].probe, CHECKS["cell labels"].probe)
    def words_stay_inside_the_box_that_holds_them(self, _name, controls, cells):
        self.assertEqual(controls, [])
        self.assertEqual(cells, [])

    @reads(CHECKS["sideways"].probe)
    def only_a_table_scrolls_sideways(self, _name, found):
        """At a phone, tablet and laptop width; again with every disclosure open."""

        self.assertEqual(found, [])

    @reads(CHECKS["sideways"].probe)
    def only_a_table_scrolls_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    @reads(CELLS)
    def nothing_runs_out_of_its_table_cell(self, _name, found):
        self.assertEqual(found, [])

    @reads(_ESCAPES_READ, OVERLAPS)
    def dense_pages_keep_to_their_boxes(self, _name, escapes, overlaps):
        """Nothing escapes or overlaps with production-sized data."""

        self.assertEqual(escapes, [])
        self.assertEqual(overlaps, [])

    @reads(TABLES)
    def tables_scroll_inside_their_own_container(self, name, tables):
        """A table wider than its card scrolls sideways in its own region; it
        never runs past the card or stacks into cards."""

        self.assertEqual(tables["found"], [])
        # The probe counts the tables it skips as undrawn (in a closed
        # disclosure) by its own test, so a page whose every table is in one
        # is told apart from a page whose tables it failed to find.
        if "<table" in (self.pages.get(name) or self.page.content()):
            self.assertGreater(tables["seen"] + tables["undrawn"], 0, "the probe found no table")

    @reads(OVERLAPS)
    def nothing_overlaps(self, _name, found):
        self.assertEqual(found, [])

    @reads(INTO_A_NEIGHBOR)
    def nothing_is_drawn_into_its_neighbor(self, _name, found):
        """What a box in a row holds stays in it: a figure's dots never reach the next figure."""

        self.assertEqual(found, [])

    @reads(CHECKS["audit"].probe)
    def layout_audit_structural_rules(self, _name, report):
        self.assertEqual(AS_FINDINGS["audit"](report), [])

    @reads((NARROW_FRAMES, SELECTORS["table_scroll"]))
    def a_table_frame_spans_its_box(self, _name, found):
        self.assertEqual(found, [])

    def a_fill_shows_in_both_palettes(self, _name):
        self.assertEqual(self.lost_fills(), [])

    def every_palette_check_holds(self, _name):
        """Text and the edges of fields and buttons against what they sit
        on, in the palette the page is drawn in."""

        if found := {check: said for check, said in self.palette().items() if said}:
            self.fail(json.dumps(found, indent=1))

    def text_reads_in_the_dark_theme(self, _name):
        """Text clears WCAG AA on whatever it sits on, in the dark palette.

        Color lives in tokens and each token carries both palettes, so a
        light-only color can only come from a component that went round them.
        The stylesheet test catches a literal; this catches the rest (a token
        whose dark half was never checked, ink meant for one surface drawn on
        another) by measuring what the browser actually paints.
        """

        dark, found = self.read([("() => matchMedia('(prefers-color-scheme: dark)').matches", None), contrast_read()])
        self.assertEqual(dark, True)
        self.assertEqual(found, [])

    def a_chosen_theme_paints_as_the_systems(self, name):
        """The operator's choice of theme paints what the system's preference
        does: each palette chosen against the other system palette paints
        every element as that palette does when the system asks for it. A
        rule written under `prefers-color-scheme` rather than through the
        tokens' `light-dark()` reaches only one of the two. The server writes
        the choice into the page it renders, so the page is loaded with it,
        and left as it was found."""

        was, differ, html = self.scheme(), [], self.page.content()
        try:
            for theme, other in (("dark", "light"), ("light", "dark")):
                self.page.emulate_media(color_scheme=theme)
                self.load_as(name, html, None)
                system = self.page.evaluate(PAINT)
                self.page.emulate_media(color_scheme=other)
                self.load_as(name, html, theme)
                chosen = self.page.evaluate(PAINT)
                differ += [
                    f"{theme} chosen paints {el} {mine}, the system's {its}"
                    for (el, mine), (_, its) in zip(chosen, system, strict=True)
                    if mine != its
                ]
        finally:
            self.page.emulate_media(color_scheme=was)
            self.load_as(name, html, None)
        self.assertEqual(differ[:10], [])

    # How long, in milliseconds, a page loaded again from its own HTML may
    # take: every part of it is answered from memory.
    reload_within = 5_000

    def load_as(self, name, html, theme):
        """Page ``name`` loaded again from ``html``, as the server renders it
        for an operator who chose ``theme`` (none: the system's), or a failure
        naming it once the load outlasts ``reload_within``."""

        if theme:
            html = re.sub(r"<html\b", f'<html data-theme="{theme}"', html, count=1)
        address = self.page.url.split("#")[0]

        def answer(route):
            self.fulfill(route, answered_as(self.pages[name], html))

        self.page.route(address, answer)
        try:
            self.page.reload(wait_until="load", timeout=self.reload_within)
        except LoadTimeout:
            chosen = f"{theme} chosen" if theme else "no theme chosen"
            self.fail(f"{name} did not load again with {chosen} within {self.reload_within} ms")
        finally:
            self.page.unroute(address, answer)
        self.page.evaluate(FINISH_TRANSITIONS)

    @reads((BROKEN_WORDS, SELECTORS["identifier"]))
    def no_column_is_narrower_than_its_words(self, _name, found):
        self.assertEqual(found, [])

    def every_layout_check_holds(self, name):
        """Every check the layout rules make, on the page as a reader has it:
        what `dev:look --rule survey` reports, held."""

        if found := self.findings(name, self.page.viewport_size["width"]):
            self.fail(json.dumps(found, indent=1))

    @reads(PAST_THE_CLAMP)
    def a_clamped_box_shows_only_its_lines(self, _name, found):
        self.assertEqual(found, [])

    @reads((CUT_DECISIONS, [SELECTORS["decision_detail"], "*", SELECTORS["unseen"]]))
    def what_an_item_means_is_never_cut(self, _name, found):
        """What a queue item means, an approval's change among them, wraps
        rather than being cut, at every width."""

        self.assertEqual(found, [])

    @reads((PAST_THE_GUTTER, [SELECTORS["gutter_bound"], False]))
    def code_and_cards_keep_inside_the_gutter(self, _name, found):
        self.assertEqual(found, [])

    @reads(EVIDENCE_READ)
    def a_checks_evidence_follows_its_statement(self, _name, found):
        self.assertEqual(found, [])

    @reads(SUMMARIES_READ)
    def nothing_a_summary_names_is_hidden(self, _name, found):
        self.assertEqual(found, [])

    # With script: the page a person reads, sorted, selectable and with its
    # cut values marked.

    def a_table_fits_its_box(self, name):
        """A table never overflows its box at the width the page stands at:
        while it is wider, its ranked columns leave, then it is narrow, then
        tight (tables.js). Every table on the page, with every disclosure open.
        """

        self.open_every_disclosure(name)
        self.page.evaluate(SETTLE)
        self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

    def no_value_is_cut_while_its_row_has_room(self, name):
        """A value in a closed row is cut only once its row has no room
        left that would show it whole: in its cell past what shares its line,
        and in the columns wider than their widest cell asks. Every table on
        the page, with every disclosure open."""

        self.open_every_disclosure(name)
        self.page.evaluate(SETTLE)
        self.assertEqual(self.read([(CUT_BESIDE_ROOM, SELECTORS["unseen"])])[0], [])

    def a_table_fits_its_box_at_every_width(self, name):
        """Every table on the page, with every disclosure open, fits its box
        and keeps every table check, its rows opened, at every window width
        from the narrowest phone up."""

        self.open_every_disclosure(name)
        self.page.evaluate(SETTLE)
        self.assertEqual(self.table_defects_at_every_width(), {})

    def card_rows(self, key):
        """The cards of one dashboard grid, grouped by the row they sit on."""
        rows: dict[int, list[dict]] = {}
        for box in self.page.locator(SELECTORS[key]).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(); return {top: r.top, left: r.left}; })"
        ):
            rows.setdefault(round(box["top"]), []).append(box)
        return list(rows.values())
