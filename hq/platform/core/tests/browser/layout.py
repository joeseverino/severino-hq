"""The layout rules held over the stress estate, which the sweeps run."""

import json
import re
from itertools import islice, takewhile
from typing import override

lazy from playwright.sync_api import TimeoutError as LoadTimeout

from hq.platform.core.tests.browser.gate import BrowserGate, answered_as
from hq.platform.core.tests.browser.probes.boxes import (
    CHROME_INSETS,
    ESCAPES,
    INTO_A_NEIGHBOR,
    NARROW_FRAMES,
    OVERLAPS,
)
from hq.platform.core.tests.browser.probes.palette import PAINT
from hq.platform.core.tests.browser.probes.settle import FINISH_TRANSITIONS, SETTLE, STYLESHEETS
from hq.platform.core.tests.browser.probes.tables import (
    CELLS,
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
from hq.platform.core.tests.browser.survey import (
    _AUDIT,
    _CONTRAST_READ,
    _SIDEWAYS_READ,
    _SMALL_TARGETS_READ,
    _SPILLED_CELLS,
    _SPILLED_CONTROLS,
    AS_FINDINGS,
    CHANGES_READ,
    CHIPS_READ,
    EVIDENCE_READ,
    HEADS_READ,
    SUMMARIES_READ,
)
from hq.platform.core.tests.browser.widths import SPARSE_WIDTHS, WIDTHS
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES
from hq.platform.core.tests.test_browser_fixtures import PAGES


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
        fails its own rule only."""

        read: dict[int, list] = {}
        for at, rule in enumerate(rules):
            if hasattr(rule, "probes") and at not in read:
                read |= self.read_ahead(rules, at)
            with self.subTest(rule=rule.__name__, page=name, width=width, **where):
                if at in read:
                    rule(name, *map(taken, read[at]))
                else:
                    rule(name)

    def read_ahead(self, rules, at):
        """The outcomes of each rule's probes, by its place in ``rules``, for
        the run of reading rules that starts at ``at``."""

        run = list(takewhile(lambda rule: hasattr(rule, "probes"), rules[at:]))
        outcomes = iter(self.outcomes([probe for rule in run for probe in rule.probes]))
        return {at + offset: list(islice(outcomes, len(rule.probes))) for offset, rule in enumerate(run)}

    def in_scheme(self, scheme):
        """The same load in the other palette. Its colors are finished rather
        than waited for: without script no animation frame runs."""

        self.page.emulate_media(color_scheme=scheme)
        self.page.evaluate(FINISH_TRANSITIONS)

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
            rules += self.overflow_reads(name)
        return rules

    def overflow_reads(self, name):
        rules = [
            self.no_block_breaks_a_paragraph,
            self.words_stay_inside_the_box_that_holds_them,
            self.only_a_table_scrolls_sideways,
            self.nothing_runs_out_of_its_table_cell,
            self.a_table_frame_spans_its_box,
            self.no_column_is_narrower_than_its_words,
            self.a_checks_evidence_follows_its_statement,
            self.nothing_a_summary_names_is_hidden,
            self.a_clamped_box_shows_only_its_lines,
            self.what_a_button_decides_is_shown_whole,
            self.a_change_reads_on_one_line_and_baseline,
            self.a_chip_sits_on_the_baseline_of_its_line,
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
                self.a_change_reads_on_one_line_and_baseline,
                self.a_chip_sits_on_the_baseline_of_its_line,
                self.a_fill_shows_in_both_palettes,
            ]
        return rules

    @reads(STYLESHEETS)
    def every_stylesheet_loads(self, _name, sheets):
        self.assertGreater(sheets["links"], 0)
        self.assertEqual(sheets["loaded"], sheets["links"])
        self.assertEqual(sheets["empty"], [])

    @reads(ESCAPES)
    def nothing_escapes_the_page_sideways(self, _name, found):
        self.assertEqual(found, [])

    @reads(CHROME_INSETS)
    def the_chrome_is_as_tall_as_its_insets_say(self, _name, found):
        self.assertEqual(found, [])

    def open_every_disclosure(self, _name):
        """Every disclosure and popover open, for the rules that follow."""

        self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")

    @reads(ESCAPES)
    def nothing_escapes_the_page_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    def no_block_breaks_a_paragraph(self, _name):
        # A block written inside a <p> closes it early and leaves an empty <p>
        # where its end tag was: the rest of the line falls out of it.
        self.assertEqual(self.page.locator(SELECTORS["broken_paragraph"]).count(), 0)

    @reads(_SPILLED_CONTROLS, _SPILLED_CELLS)
    def words_stay_inside_the_box_that_holds_them(self, _name, controls, cells):
        self.assertEqual(controls, [])
        self.assertEqual(cells, [])

    @reads(_SIDEWAYS_READ)
    def only_a_table_scrolls_sideways(self, _name, found):
        """At a phone, tablet and laptop width; again with every disclosure open."""

        self.assertEqual(found, [])

    @reads(_SIDEWAYS_READ)
    def only_a_table_scrolls_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    @reads(CELLS)
    def nothing_runs_out_of_its_table_cell(self, _name, found):
        self.assertEqual(found, [])

    @reads(ESCAPES, OVERLAPS)
    def dense_pages_keep_to_their_boxes(self, _name, escapes, overlaps):
        """Nothing escapes or overlaps with production-sized data."""

        self.assertEqual(escapes, [])
        self.assertEqual(overlaps, [])

    @reads(TABLES)
    def tables_scroll_inside_their_own_container(self, name, tables):
        """On a phone a wide table scrolls sideways; it never stacks into cards."""

        self.assertEqual(tables["found"], [])
        if "<table" in self.pages[name]:
            self.assertGreater(tables["seen"], 0, "the probe measured nothing")

    @reads(OVERLAPS)
    def nothing_overlaps(self, _name, found):
        self.assertEqual(found, [])

    @reads(INTO_A_NEIGHBOR)
    def nothing_is_drawn_into_its_neighbor(self, _name, found):
        """What a box in a row holds stays in it: a figure's dots never reach the next figure."""

        self.assertEqual(found, [])

    @reads(_AUDIT)
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

    @reads("() => matchMedia('(prefers-color-scheme: dark)').matches", _CONTRAST_READ)
    def text_reads_in_the_dark_theme(self, _name, dark, report):
        """Text clears WCAG AA on whatever it sits on, in the dark palette.

        Color lives in tokens and each token carries both palettes, so a
        light-only color can only come from a component that went round them.
        The stylesheet test catches a literal; this catches the rest (a token
        whose dark half was never checked, ink meant for one surface drawn on
        another) by measuring what the browser actually paints.
        """

        self.assertEqual(dark, True)
        self.assertGreater(report["sampled"], 0)
        self.assertEqual(report["found"], [])

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

    @reads(BROKEN_WORDS)
    def no_column_is_narrower_than_its_words(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SMALL_TARGETS_READ)
    def every_control_is_big_enough_to_hit(self, _name, found):
        """A target is hit through at least 24 by 24 CSS px (`TARGET_MIN`,
        WCAG 2.5.8 Target Size Minimum), counting the area its `::before` or
        `::after` hangs over the space beside it, unless one of the
        criterion's exceptions holds:

        - Spacing: "if a 24 CSS pixel diameter circle is centered on the
          bounding box of each, the circles do not intersect another target or
          the circle for another undersized target". An area a target hangs
          over another target's is shared, and counts for neither.
        - Equivalent: "The function can be achieved through a different
          control on the same page that meets this criterion": the control
          a target names in `data-target-equivalent` (by its id) is at least
          24 by 24 and in the same cell or region.
        - Inline: "The target is in a sentence or its size is otherwise
          constrained by the line-height of non-target text": set inline
          among words that are no target's.
        - User agent control: "The size of the target is determined by the
          user agent and is not modified by the author": a control the browser
          draws, the size of a copy no author style reaches. One HQ draws or
          sizes, a styled tick box included, is held to the size.

        A target another element is drawn over at its center (an open menu
        lying on the page) is not reachable there, so it is not measured,
        and neither is a disabled one or one an open modal makes inert.
        A tick box is hit through its labels too. The stylesheet's
        `--target-min` is held to the same size. Held on every page and width
        the layout checks read (`every_layout_check_holds`).
        """

        if found:
            self.fail("\n".join(found))

    def every_layout_check_holds(self, name):
        """Every check the layout rules make, on the page as a reader has it:
        what `dev:look --rule survey` reports, held."""

        if found := self.findings(name, self.page.viewport_size["width"]):
            self.fail(json.dumps(found, indent=1))

    @reads(CHANGES_READ)
    def a_change_reads_on_one_line_and_baseline(self, _name, found):
        """A value and the one it changes into share a baseline with each
        other, their arrow and the words before them wherever they share a
        line, and the arrow is on the line of the new value: a narrow line
        breaks between the two values, never after the arrow."""

        self.assertEqual(found, [])

    @reads(CHIPS_READ)
    def a_chip_sits_on_the_baseline_of_its_line(self, _name, found):
        """A chip (an identifier, a tag) set in a line sits on the baseline
        of the words, pills and links around it, on the line it starts on and
        the line it ends on."""

        self.assertEqual(found, [])

    @reads(HEADS_READ)
    def a_head_keeps_two_controls_beside_its_title(self, _name, found):
        """A page's head draws its lead control and one more beside the title
        on one line with its menu, folding the second into the menu where the
        head is narrow, and offers every control once: beside the title or in
        the menu, never both and never neither."""

        self.assertEqual(found, [])

    @reads(PAST_THE_CLAMP)
    def a_clamped_box_shows_only_its_lines(self, _name, found):
        self.assertEqual(found, [])

    @reads((CUT_DECISIONS, SELECTORS["decision_whole"]))
    def what_a_button_decides_is_shown_whole(self, _name, found):
        """An approval's change, beside the buttons that approve or reject
        it, wraps rather than being cut, at every width."""

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

    def a_table_fits_its_box_at_every_width(self, name):
        """A table never overflows its box, down to the narrowest phone: while
        it is wider, its ranked columns leave, then it is narrow, then tight
        (tables.js). Every table on every page, with every disclosure open.
        """

        self.open_every_disclosure(name)
        self.page.evaluate(SETTLE)
        self.assertEqual(self.page.evaluate(TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

    def card_rows(self, key):
        """The cards of one dashboard grid, grouped by the row they sit on."""
        rows: dict[int, list[dict]] = {}
        for box in self.page.locator(SELECTORS[key]).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(); return {top: r.top, left: r.left}; })"
        ):
            rows.setdefault(round(box["top"]), []).append(box)
        return list(rows.values())
