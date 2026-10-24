"""The layout rules held over the stress estate, which the sweeps run."""

import json
import re
from itertools import islice, takewhile
from typing import override

lazy from playwright.sync_api import TimeoutError as LoadTimeout

from hq.platform.core.tests.browser.gate import BrowserGate, answered_as
from hq.platform.core.tests.browser.probes.boxes import (
    _ESCAPES,
    _FIGURE_UNDER_LABEL,
    _NARROW_FRAMES,
    _OFF_CENTER_MARKS,
    _OVERLAPS,
    _PAIRS,
    _UNEVEN_BANDS,
    _UNPADDED_TILES,
)
from hq.platform.core.tests.browser.probes.cut_names import _PADDED_CUTS, _PAST_THE_CLAMP
from hq.platform.core.tests.browser.probes.palette import _PAINT
from hq.platform.core.tests.browser.probes.parts import (
    _HEAD_ON_ONE_LINE,
    _LONE_ARROWS,
    _MOVED_BY_SPEAKING,
    _MOVING_SUMMARIES,
    _NESTED_DISCLOSURES,
    _RAGGED_BARS,
    _RAGGED_QUEUES,
    _RAISED_LINKS,
    _SUMMARY_MARKS,
    _UNEVEN_CONTROL_ROWS,
    _UNEVEN_ROUTES,
    _WRAPPED_ROUTES,
)
from hq.platform.core.tests.browser.probes.settle import _FINISH_TRANSITIONS, _SETTLED, _STYLESHEETS
from hq.platform.core.tests.browser.probes.tables import (
    _CELLS,
    _CELLS_OFF_THE_FIRST_LINE,
    _NAME_EDGES,
    _PAST_THE_GUTTER,
    _ROW_HEIGHTS,
    _TABLES,
    _TABLES_THAT_DO_NOT_FIT,
    _UNPADDED_CELLS,
    ROW_LINES,
)
from hq.platform.core.tests.browser.probes.text import (
    _ASIDE_BESIDE_ITS_HEADING,
    _BROKEN_WORDS,
    _LONE_ASIDE_WORDS,
    _SQUEEZED_TITLES,
)
from hq.platform.core.tests.browser.probes.tokens import _SCRAP, _SCRAPS, _STRANDED_MARKS, _TOKEN_BREAKS
from hq.platform.core.tests.browser.reading import reads, taken
from hq.platform.core.tests.browser.selectors import _TOKEN_SELECTORS, SELECTORS
from hq.platform.core.tests.browser.stress import ROUTE_PAGES, stress_estate
from hq.platform.core.tests.browser.survey import (
    _AS_FINDINGS,
    _AUDIT,
    _CONTRAST_READ,
    _CUT_NAMES_READ,
    _CUT_WITH_ROOM_READ,
    _EMPTY_FRAMES_READ,
    _EVIDENCE_READ,
    _FORM_ROWS_READ,
    _FRAMES_READ,
    _LINES_READ,
    _PHRASES_READ,
    _ROUTE_ENDS_READ,
    _SIDEWAYS_READ,
    _SMALL_TARGETS_READ,
    _SPILLED_CELLS,
    _SPILLED_CONTROLS,
    _SPLIT_NAMES_READ,
    _SUMMARIES_READ,
    _UNDER_LABELS_READ,
)
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, SPARSE_WIDTHS, WIDTHS
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES
from hq.platform.core.tests.test_browser_fixtures import PAGES

# What a pressed control says about its work, short to long.
SAYINGS = ("Reading", "Reading example-host", "That could not be done. Try again in a minute.")


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
    # The controls that spoke, counted by the sweep that presses them.
    spoke: int

    @classmethod
    @override
    def render(cls):
        pages, cls.lists = stress_estate()
        return pages

    def loaded(self, name, width, **where):
        """Open ``name`` at ``width``; one that does not load fails once, not once per rule."""

        opened = False
        # subTest records a failure and carries on, so `opened` stays False.
        with self.subTest(page=name, width=width, **where):
            self.open(name, width)
            opened = True
        return opened

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
        self.page.evaluate(_FINISH_TRANSITIONS)

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
                self.grid_rows_end_together,
                self.a_pill_never_wraps,
                self.nothing_overlaps,
                self.layout_audit_structural_rules,
            ]
        if width in WIDTHS:
            rules += self.overflow_reads(name)
        if width == PHONE_SCREEN[0]:
            rules.append(self.a_narrow_head_keeps_its_lead_control_beside_the_title)
        if width == WIDTHS[-1]:
            rules.append(self.a_wide_frame_is_filled)
        return rules

    def overflow_reads(self, name):
        rules = [
            self.no_block_breaks_a_paragraph,
            self.nothing_is_framed_inside_a_frame,
            self.words_stay_inside_the_box_that_holds_them,
            self.only_a_table_scrolls_sideways,
            self.nothing_runs_out_of_its_table_cell,
            self.a_table_frame_spans_its_box,
            self.a_table_row_stays_within_its_height_budget,
            self.a_row_reads_from_its_first_line,
            self.no_column_is_narrower_than_its_words,
            self.a_link_sits_on_the_line_it_continues,
            self.a_row_has_one_disclosure,
            self.a_summary_draws_one_mark,
            self.a_band_cell_keeps_its_padding,
            self.a_band_lays_out_as_whole_rows,
            self.a_queue_ends_its_cards_at_one_edge,
            self.a_route_line_runs_from_its_first_stop_to_its_last,
            self.a_mark_holds_its_glyph_in_its_middle,
            self.a_checks_evidence_follows_its_statement,
            self.nothing_a_summary_names_is_hidden,
            self.a_token_breaks_only_where_it_may,
            self.no_token_leaves_a_scrap_on_a_line,
            self.a_mark_keeps_to_the_words_it_punctuates,
            self.a_name_in_a_wide_table_is_whole,
            self.a_short_name_in_a_wide_table_is_one_line,
            self.nothing_is_held_back_while_its_table_has_room,
            self.a_figure_sits_under_its_label,
            self.a_value_sits_beside_its_label,
            self.an_aside_wraps_only_under_its_heading,
            self.an_aside_leaves_no_word_alone,
            self.a_clamped_box_shows_only_its_lines,
            self.a_title_is_not_split_beside_its_controls,
            self.code_and_cards_keep_inside_the_gutter,
            self.controls_in_a_row_share_one_height,
            self.a_wrapped_bar_fills_its_rows,
            self.a_form_fills_its_rows,
        ]
        if name in DENSE_PAGES:
            rules.append(self.dense_pages_keep_to_their_boxes)
        if name in ROUTE_PAGES:
            rules.extend((self.a_route_is_one_row_or_one_column, self.a_route_across_spaces_its_hops_evenly))
        return rules

    def unscripted_openings(self, name, width):
        """The rules that open the page's disclosures, after every read."""

        rules = []
        if width in WIDTHS:
            rules.append(self.a_filled_box_keeps_its_padding)
        rules.append(self.open_every_disclosure)
        if name in PAGES and width in SPARSE_WIDTHS:
            rules.append(self.nothing_escapes_the_page_sideways_with_every_disclosure_open)
        if width in WIDTHS:
            rules += [
                self.only_a_table_scrolls_sideways_with_every_disclosure_open,
                self.a_route_line_runs_from_its_first_stop_to_its_last,
                self.a_checks_evidence_follows_its_statement,
                self.a_value_sits_beside_its_label,
                self.nothing_a_summary_names_is_hidden,
                self.a_line_shows_on_its_surface,
                self.a_fill_shows_in_both_palettes,
            ]
        return rules

    @reads(_STYLESHEETS)
    def every_stylesheet_loads(self, _name, sheets):
        self.assertGreater(sheets["links"], 0)
        self.assertEqual(sheets["loaded"], sheets["links"])
        self.assertEqual(sheets["empty"], [])

    @reads(_ESCAPES)
    def nothing_escapes_the_page_sideways(self, _name, found):
        self.assertEqual(found, [])

    def open_every_disclosure(self, _name):
        """Every disclosure and popover open, for the rules that follow."""

        self.page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")

    @reads(_ESCAPES)
    def nothing_escapes_the_page_sideways_with_every_disclosure_open(self, _name, found):
        self.assertEqual(found, [])

    def no_block_breaks_a_paragraph(self, _name):
        # A block written inside a <p> closes it early and leaves an empty <p>
        # where its end tag was: the rest of the line falls out of it.
        self.assertEqual(self.page.locator(SELECTORS["broken_paragraph"]).count(), 0)

    @reads(_FRAMES_READ)
    def nothing_is_framed_inside_a_frame(self, _name, found):
        self.assertEqual(found, [])

    @reads((_HEAD_ON_ONE_LINE, [SELECTORS["head_lead"], SELECTORS["head_title"], SELECTORS["page_head"]]))
    def a_narrow_head_keeps_its_lead_control_beside_the_title(self, _name, beside):
        self.assertIn(beside, (None, True))

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

    @reads(_CELLS)
    def nothing_runs_out_of_its_table_cell(self, _name, found):
        self.assertEqual(found, [])

    @reads((_ROW_HEIGHTS, ROW_LINES))
    def a_table_row_stays_within_its_height_budget(self, _name, found):
        self.assertEqual(found, [])

    @reads((_CELLS_OFF_THE_FIRST_LINE, SELECTORS["table_scroll"]))
    def a_row_reads_from_its_first_line(self, _name, found):
        self.assertEqual(found, [])

    @reads(_ESCAPES, _OVERLAPS, _PHRASES_READ)
    def dense_pages_keep_to_their_boxes(self, _name, escapes, overlaps, phrases):
        """Nothing escapes, overlaps, or wraps a pill with production-sized data."""

        self.assertEqual(escapes, [])
        self.assertEqual(overlaps, [])
        self.assertEqual(phrases, [])

    @reads(_TABLES)
    def tables_scroll_inside_their_own_container(self, name, tables):
        """On a phone a wide table scrolls sideways; it never stacks into cards."""

        self.assertEqual(tables["found"], [])
        if "<table" in self.pages[name]:
            self.assertGreater(tables["seen"], 0, "the probe measured nothing")

    @reads((_PAIRS, SELECTORS["paired"]))
    def grid_rows_end_together(self, _name, found):
        self.assertEqual(found, [])

    @reads(_PHRASES_READ)
    def a_pill_never_wraps(self, _name, found):
        self.assertEqual(found, [])

    @reads(_OVERLAPS)
    def nothing_overlaps(self, _name, found):
        self.assertEqual(found, [])

    @reads(_AUDIT)
    def layout_audit_structural_rules(self, _name, report):
        self.assertEqual(_AS_FINDINGS["audit"](report), [])

    @reads((_NARROW_FRAMES, SELECTORS["table_scroll"]))
    def a_table_frame_spans_its_box(self, _name, found):
        self.assertEqual(found, [])

    @reads((_OFF_CENTER_MARKS, SELECTORS["route_mark"]))
    def a_mark_holds_its_glyph_in_its_middle(self, _name, found):
        self.assertEqual(found, [])

    @reads(_MOVING_SUMMARIES)
    def opening_a_disclosure_moves_nothing_before_it(self, _name, found):
        self.assertEqual(found, [])

    @reads(_LINES_READ)
    def a_line_shows_on_its_surface(self, _name, found):
        self.assertEqual(found, [])

    def a_fill_shows_in_both_palettes(self, _name):
        self.assertEqual(self.lost_fills(), [])

    def every_palette_check_holds(self, _name):
        """Text, lines, fills and frames against what they sit on, in the
        palette the page is drawn in."""

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
                system = self.page.evaluate(_PAINT)
                self.page.emulate_media(color_scheme=other)
                self.load_as(name, html, theme)
                chosen = self.page.evaluate(_PAINT)
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
        self.page.evaluate(_FINISH_TRANSITIONS)

    @reads(
        (
            _UNPADDED_TILES,
            [SELECTORS["tile_exempt"], SELECTORS["tile_control"], SELECTORS["tile_detached"], SELECTORS["disclosure"]],
        )
    )
    def a_filled_box_keeps_its_padding(self, _name, found):
        """It opens every disclosure in the page to lay out what they hold."""

        self.assertEqual(found, [])

    @reads(_BROKEN_WORDS)
    def no_column_is_narrower_than_its_words(self, _name, found):
        self.assertEqual(found, [])

    @reads(_RAISED_LINKS)
    def a_link_sits_on_the_line_it_continues(self, _name, found):
        self.assertEqual(found, [])

    @reads((_LONE_ARROWS, SELECTORS["external_link"]))
    def an_external_links_arrow_keeps_to_its_last_word(self, _name, found):
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

    @reads(_EMPTY_FRAMES_READ)
    def a_wide_frame_is_filled(self, _name, found):
        self.assertEqual(found, [])

    @reads(_NESTED_DISCLOSURES)
    def a_row_has_one_disclosure(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SUMMARY_MARKS)
    def a_summary_draws_one_mark(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNPADDED_CELLS, SELECTORS["band_cell"]))
    def a_band_cell_keeps_its_padding(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNEVEN_BANDS, [SELECTORS["band"], SELECTORS["band_aside"]]))
    def a_band_lays_out_as_whole_rows(self, _name, found):
        self.assertEqual(found, [])

    @reads((_WRAPPED_ROUTES, SELECTORS["route"]))
    def a_route_is_one_row_or_one_column(self, _name, found):
        self.assertEqual(found, [])

    @reads((_FIGURE_UNDER_LABEL, [SELECTORS[key] for key in ("figure", "figure_label", "figure_value")]))
    def a_figure_sits_under_its_label(self, _name, found):
        self.assertEqual(found, [])

    @reads(_UNDER_LABELS_READ)
    def a_value_sits_beside_its_label(self, _name, found):
        """Closed, and again with every disclosure open."""

        self.assertEqual(found, [])

    @reads((_TOKEN_BREAKS, _TOKEN_SELECTORS))
    def a_token_breaks_only_where_it_may(self, _name, found):
        self.assertEqual(found, [])

    @reads((_SCRAPS, [SELECTORS["any_token"], _SCRAP]))
    def no_token_leaves_a_scrap_on_a_line(self, _name, found):
        self.assertEqual(found, [])

    @reads((_STRANDED_MARKS, SELECTORS["seam"]))
    def a_mark_keeps_to_the_words_it_punctuates(self, _name, found):
        self.assertEqual(found, [])

    @reads(_CUT_NAMES_READ)
    def a_name_in_a_wide_table_is_whole(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SPLIT_NAMES_READ)
    def a_short_name_in_a_wide_table_is_one_line(self, _name, found):
        """A wide table is squeezed most just past WIDE_REGION: at WIDE_TABLES."""

        self.assertEqual(found, [])

    @reads(_CUT_WITH_ROOM_READ)
    def nothing_is_held_back_while_its_table_has_room(self, _name, found):
        self.assertEqual(found, [])

    @reads((_PADDED_CUTS, SELECTORS["cut_box"]))
    def a_cut_value_ends_where_its_words_end(self, _name, found):
        self.assertEqual(found, [])

    def every_layout_check_holds(self, name):
        """Every check the layout rules make, on the page as a reader has it:
        what `dev:look --rule survey` reports, held."""

        if found := self.findings(name, self.page.viewport_size["width"]):
            self.fail(json.dumps(found, indent=1))

    @reads((_ASIDE_BESIDE_ITS_HEADING, SELECTORS["heading_aside"]))
    def an_aside_wraps_only_under_its_heading(self, _name, found):
        self.assertEqual(found, [])

    @reads((_LONE_ASIDE_WORDS, SELECTORS["any_aside"]))
    def an_aside_leaves_no_word_alone(self, _name, found):
        self.assertEqual(found, [])

    @reads(_PAST_THE_CLAMP)
    def a_clamped_box_shows_only_its_lines(self, _name, found):
        self.assertEqual(found, [])

    @reads((_SQUEEZED_TITLES, [SELECTORS["page_title"], SELECTORS["page_actions"], SELECTORS["page_head"]]))
    def a_title_is_not_split_beside_its_controls(self, _name, found):
        self.assertEqual(found, [])

    @reads((_PAST_THE_GUTTER, [SELECTORS["gutter_bound"], False]))
    def code_and_cards_keep_inside_the_gutter(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNEVEN_CONTROL_ROWS, [SELECTORS["line_control"], SELECTORS["field"]]))
    def controls_in_a_row_share_one_height(self, _name, found):
        self.assertEqual(found, [])

    @reads((_RAGGED_BARS, SELECTORS["wrapping_bar"]))
    def a_wrapped_bar_fills_its_rows(self, _name, found):
        self.assertEqual(found, [])

    @reads(_FORM_ROWS_READ)
    def a_form_fills_its_rows(self, _name, found):
        self.assertEqual(found, [])

    @reads((_RAGGED_QUEUES, [SELECTORS["queue_card"], SELECTORS["queue_card_end"]]))
    def a_queue_ends_its_cards_at_one_edge(self, _name, found):
        self.assertEqual(found, [])

    @reads((_UNEVEN_ROUTES, [SELECTORS["route"], SELECTORS["route_icon"]]))
    def a_route_across_spaces_its_hops_evenly(self, _name, found):
        self.assertEqual(found, [])

    @reads(_ROUTE_ENDS_READ)
    def a_route_line_runs_from_its_first_stop_to_its_last(self, _name, found):
        self.assertEqual(found, [])

    @reads(_EVIDENCE_READ)
    def a_checks_evidence_follows_its_statement(self, _name, found):
        self.assertEqual(found, [])

    @reads(_SUMMARIES_READ)
    def nothing_a_summary_names_is_hidden(self, _name, found):
        self.assertEqual(found, [])

    # With script: the page a person reads, sorted, selectable and with its
    # cut values marked.

    def scripted_reads(self):
        """What a scripted page holds beside `every_layout_check_holds`."""

        return [
            self.opening_a_disclosure_moves_nothing_before_it,
            self.a_tables_names_share_one_edge_whether_or_not_a_row_opens,
        ]

    @reads((_NAME_EDGES, SELECTORS["table_scroll"]))
    def a_tables_names_share_one_edge_whether_or_not_a_row_opens(self, _name, tables):
        """A row with a toggle and a row without start their names on the same
        line down the column: the toggle has a gutter every row keeps.
        """

        for edges in tables:
            self.assertEqual(edges, 1)

    def pressing_a_control_moves_nothing(self, name, width):
        """Each saying is drawn and then taken back, so the next is measured
        on the page as it loaded."""

        for said in SAYINGS:
            with self.subTest(rule="pressing_a_control_moves_nothing", page=name, width=width, said=said):
                found = self.page.evaluate(_MOVED_BY_SPEAKING, [said, SELECTORS["spoken"]])
                if found is None:
                    continue
                self.spoke += 1
                self.assertEqual(found, [])

    def a_table_fits_its_box_at_every_width(self, name):
        """A table never overflows its box, down to the narrowest phone: while
        it is wider, its ranked columns leave, then it is narrow, then tight
        (tables.js). Every table on every page, with every disclosure open.
        """

        self.open_every_disclosure(name)
        self.page.evaluate(_SETTLED)
        self.assertEqual(self.page.evaluate(_TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]), [])

    def wait_for_parts(self, route):
        if route.request.headers.get("x-requested-with") != "XMLHttpRequest":
            route.fallback()

    def loaded_and_still(self):
        self.page.wait_for_load_state("load")
        self.settle()
        self.page.evaluate(_SETTLED)

    def card_rows(self, key):
        """The cards of one dashboard grid, grouped by the row they sit on."""
        rows: dict[int, list[dict]] = {}
        for box in self.page.locator(SELECTORS[key]).evaluate_all(
            "elements => elements.map(e => { const r = e.getBoundingClientRect(), "
            "edge = e.parentElement.getBoundingClientRect().right; "
            "return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, edge}; })"
        ):
            rows.setdefault(round(box["top"]), []).append(box)
        return list(rows.values())

    def assert_rows_filled(self, key):
        # Cards in a row are the same height, and the last one reaches the
        # grid's right edge.
        for row in self.card_rows(key):
            self.assertEqual({round(box["bottom"]) for box in row}, {round(row[0]["bottom"])}, row)
            self.assertAlmostEqual(row[-1]["right"], row[-1]["edge"], delta=1)
