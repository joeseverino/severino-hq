"""The layout sweeps: the stress estate's pages without script, with it, settled, and at their longest."""

from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.settle import (
    _CLOSE_AGAIN,
    _FINISH_TRANSITIONS,
    _KEEP_CLOSED,
    _PRESS_EVERY_SHOWN,
    _SETTLED,
)
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.stress import ALL_PAGES, SHARED_PARTS, SPEAKING_HEADS
from hq.platform.core.tests.browser.widths import (
    NARROWEST,
    SPARSE_WIDTHS,
    SPEAKING_WIDTHS,
    WIDE,
    WIDE_TABLES,
    WIDTHS,
    viewport,
)
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES
from hq.platform.core.tests.test_browser_fixtures import PAGES


class UnscriptedLayoutTests(LayoutBrowserTests):
    """Every page without script: the page as the server drew it.

    Without script nothing on a page reads the width it was loaded at, so a
    page is loaded once, at its narrowest width, and resized for the rest
    (``across``): what a rule opened is closed again before the next width.
    The sweep is split between the classes below.
    """

    def sweep(self, names):
        for name in names:
            for width in self.across(name, {*WIDTHS, *(SPARSE_WIDTHS if name in PAGES else ())}):
                self.hold(name, width, self.unscripted_reads(name, width))
                if width in WIDTHS:
                    self.in_scheme("dark")
                    self.hold(name, width, [self.text_reads_in_the_dark_theme, self.a_line_shows_on_its_surface])
                    self.in_scheme("light")
                if width == WIDE:
                    self.hold(name, width, [self.a_chosen_theme_paints_as_the_systems])
                self.hold(name, width, self.unscripted_openings(name, width))

    def across(self, name, widths):
        """``name`` at each of ``widths``, narrowest first, from one load; a
        page that does not load fails once."""

        first, *rest = sorted(widths)
        if not self.loaded(name, first):
            return
        self.page.evaluate(_KEEP_CLOSED)
        yield first
        for width in rest:
            self.page.evaluate(_CLOSE_AGAIN)
            self.page.set_viewport_size(viewport(width))
            # A rule the new width changes may transition; the load it stands
            # for would have drawn it finished.
            self.page.evaluate(_FINISH_TRANSITIONS)
            yield width


class UnscriptedSparseLayoutTests(UnscriptedLayoutTests):
    def test_every_sparse_page_keeps_the_layout_rules_without_script(self):
        self.sweep(PAGES)

    def test_a_shared_part_escapes_nothing_sideways(self):
        """A component filled the way an extension fills it, closed and opened."""

        for name in SHARED_PARTS:
            for width in self.across(name, SPARSE_WIDTHS):
                self.hold(
                    name,
                    width,
                    [
                        self.nothing_escapes_the_page_sideways,
                        self.open_every_disclosure,
                        self.nothing_escapes_the_page_sideways_with_every_disclosure_open,
                    ],
                )


class UnscriptedDenseLayoutTests(UnscriptedLayoutTests):
    def test_every_dense_page_keeps_the_layout_rules_without_script(self):
        self.sweep(DENSE_PAGES)

    def test_every_list_keeps_its_rows_to_their_height_without_script(self):
        """Every list filled with the longest values its model holds, as the
        server drew it."""

        for name in self.lists:
            for width in self.across(name, {*WIDTHS, *WIDE_TABLES}):
                self.hold(name, width, [self.a_table_row_stays_within_its_height_budget])


class ScriptedLayoutTests(LayoutBrowserTests):
    """Every page once its script has run. The sweep is split between the
    classes below, so workers run it side by side."""

    scripted = True

    def sweep(self, names):
        """Every layout check holds, at a laptop window's width too; pressing
        a control moves nothing; a table fits its box with every disclosure
        open. Script reads the width a page loads at, so each width is a load."""

        self.spoke = 0
        for name in names:
            widths = SPEAKING_WIDTHS if name in SPEAKING_HEADS else (*WIDTHS, *SPEAKING_WIDTHS, *WIDE_TABLES)
            for width in sorted(set(widths)):
                if self.loaded(name, width, script="on"):
                    self.page.evaluate(_SETTLED)
                    self.read_loaded(name, width)

    def read_loaded(self, name, width):
        read = name in ALL_PAGES and width in (*WIDTHS, *WIDE_TABLES)
        if read and width in WIDTHS:
            self.hold(name, width, self.scripted_reads(), script="on")
            # Light is held by every layout check below.
            self.in_scheme("dark")
            self.hold(name, width, [self.every_palette_check_holds], script="on", scheme="dark")
            self.in_scheme("light")
        if read:
            self.hold(name, width, [self.every_layout_check_holds], script="on")
        if width in SPEAKING_WIDTHS:
            self.pressing_a_control_moves_nothing(name, width)
        if name in ALL_PAGES:
            self.hold(name, width, [self.a_table_fits_its_box_at_every_width], script="on")


class ScriptedSparseLayoutTests(ScriptedLayoutTests):
    def test_every_sparse_page_keeps_the_layout_rules_once_its_script_has_run(self):
        self.sweep(PAGES)


# The dense pages in two halves, every other page, each a class of its own:
# swept together they are the longest class a layout leg runs.
FIRST_DENSE, SECOND_DENSE = tuple(DENSE_PAGES)[::2], tuple(DENSE_PAGES)[1::2]


class ScriptedDenseFirstLayoutTests(ScriptedLayoutTests):
    def test_the_first_dense_pages_and_every_head_keep_the_layout_rules_once_their_script_has_run(self):
        self.sweep((*FIRST_DENSE, *SPEAKING_HEADS))
        self.assertGreater(self.spoke, len(SPEAKING_HEADS), "no page in the fixture has a control that speaks")


class ScriptedDenseSecondLayoutTests(ScriptedLayoutTests):
    def test_the_second_dense_pages_keep_the_layout_rules_once_their_script_has_run(self):
        self.sweep(SECOND_DENSE)


class SettledLayoutTests(LayoutBrowserTests):
    """What the scripts leave once a page is drawn: nothing moved. The sweep
    is split between the classes below."""

    scripted = True

    def sweep(self, names):
        """What the server draws is where things stay. The stress estate
        answers no part a page fetches, so those requests are left waiting:
        a placeholder stays a placeholder, and what moves is the scripts'."""

        self.page.route("**/*", self.wait_for_parts)
        for name in names:
            for width in WIDTHS:
                with self.subTest(page=name, width=width):
                    moved = self.moved_once_drawn(f"{ORIGIN}/{name}/", width, self.loaded_and_still)
                    if moved:
                        self.fail("\n".join(moved))


class SettledSparseLayoutTests(SettledLayoutTests):
    def test_nothing_moves_once_a_sparse_page_is_drawn(self):
        self.sweep(PAGES)


class SettledDenseLayoutTests(SettledLayoutTests):
    def test_nothing_moves_once_a_dense_page_is_drawn(self):
        self.sweep(DENSE_PAGES)


class LongestValueLayoutTests(LayoutBrowserTests):
    """Every list in its box with the longest values its model holds."""

    scripted = True

    def test_every_list_fits_its_box_with_the_longest_values_its_model_holds(self):
        """Every list, its rows derived from its model's fields
        (test_browser_derived_fixtures), as it loads and with every row open."""

        held = [self.a_table_fits_its_box_at_every_width, self.a_token_breaks_only_where_it_may]
        for name in self.lists:
            for width in (NARROWEST, *WIDTHS, *WIDE_TABLES):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(_SETTLED)
                if width == NARROWEST:
                    self.hold(name, width, [self.a_table_fits_its_box_at_every_width], script="on")
                    continue
                self.hold(name, width, [self.every_layout_check_holds, *held], script="on")
                self.page.locator(SELECTORS["row_toggle"]).evaluate_all(_PRESS_EVERY_SHOWN)
                self.page.evaluate(_SETTLED)
                self.hold(name, width, [self.nothing_runs_out_of_its_table_cell, *held], script="on", rows="open")
