"""The layout sweeps: the stress estate's pages without script, with it, and at their longest."""

from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.settle import (
    CLOSE_AGAIN,
    FINISH_TRANSITIONS,
    KEEP_CLOSED,
    PRESS_EVERY_SHOWN,
    SETTLE,
)
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.widths import (
    NARROWEST,
    PALETTE_AT,
    WIDE,
    WIDE_TABLES,
    WIDTHS,
    viewport,
    with_wide_tables,
)
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES
from hq.platform.core.tests.test_browser_fixtures import PAGES


class UnscriptedLayoutTests(LayoutBrowserTests):
    """Every page without script: the page as the server drew it.

    Without script nothing on a page reads the width it was loaded at, so a
    page is loaded once, at its narrowest width, and resized for the rest
    (``across``): what a rule opened is closed again before the next width.
    """

    def sweep(self, names):
        for name in self.mine(names):
            for width in self.across(name, WIDTHS):
                self.hold(name, width, self.unscripted_reads(name, width))
                if width in PALETTE_AT:
                    was = self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], scheme="dark")
                    self.in_scheme(was)
                if width == WIDE:
                    self.hold(name, width, [self.a_chosen_theme_paints_as_the_systems])
                self.hold(name, width, self.unscripted_openings(name, width))

    def across(self, name, widths):
        """``name`` at each of ``widths``, narrowest first, from one load; a
        page that does not load fails once."""

        first, *rest = sorted(widths)
        if not self.loaded(name, first):
            return
        self.page.evaluate(KEEP_CLOSED)
        yield first
        for width in rest:
            self.page.evaluate(CLOSE_AGAIN)
            self.page.set_viewport_size(viewport(width))
            # A rule the new width changes may transition; the load it stands
            # for would have drawn it finished.
            self.page.evaluate(FINISH_TRANSITIONS)
            yield width


class UnscriptedSparseLayoutTests(UnscriptedLayoutTests):
    @sweep
    def test_every_sparse_page_keeps_the_layout_rules_without_script(self):
        self.sweep(PAGES)


class UnscriptedDenseLayoutTests(UnscriptedLayoutTests):
    @sweep
    def test_every_dense_page_keeps_the_layout_rules_without_script(self):
        self.sweep(DENSE_PAGES)


class ScriptedLayoutTests(LayoutBrowserTests):
    """Every page once its script has run."""

    scripted = True

    def sweep(self, names):
        """Every layout check holds, at the laptop window where a wide table
        has the least room too. Script reads the width a page loads at, so
        each width is a load."""

        for name in self.mine(names):
            for width in with_wide_tables(WIDTHS, self.draws_a_table):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(SETTLE)
                if width in PALETTE_AT:
                    # Light is held by every layout check below.
                    was = self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], script="on", scheme="dark")
                    self.in_scheme(was)
                self.hold(name, width, [self.every_layout_check_holds], script="on")


class ScriptedSparseLayoutTests(ScriptedLayoutTests):
    @sweep
    def test_every_sparse_page_keeps_the_layout_rules_once_its_script_has_run(self):
        self.sweep(PAGES)


class ScriptedDenseLayoutTests(ScriptedLayoutTests):
    @sweep
    def test_every_dense_page_keeps_the_layout_rules_once_its_script_has_run(self):
        self.sweep(DENSE_PAGES)


class LongestValueLayoutTests(LayoutBrowserTests):
    """Every list in its box with the longest values its model holds."""

    scripted = True

    @sweep
    def test_every_list_fits_its_box_with_the_longest_values_its_model_holds(self):
        """Every list, its rows derived from its model's fields
        (test_browser_derived_fixtures), as it loads and with every row open."""

        held = [self.a_table_fits_its_box]
        for name in self.mine(self.lists):
            for width in (NARROWEST, *WIDTHS, *WIDE_TABLES):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(SETTLE)
                if width == NARROWEST:
                    self.hold(name, width, [self.a_table_fits_its_box_at_every_width], script="on")
                    continue
                self.hold(name, width, [self.every_layout_check_holds, *held], script="on")
                self.page.locator(SELECTORS["row_toggle"]).evaluate_all(PRESS_EVERY_SHOWN)
                self.page.evaluate(SETTLE)
                self.hold(name, width, [self.nothing_runs_out_of_its_table_cell, *held], script="on", rows="open")
