"""The layout sweeps: the crowded estate's pages without script, with it, and the derived lists at their longest."""

from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.settle import FINISH_TRANSITIONS, PRESS_EVERY_SHOWN, SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.widths import (
    DENSE,
    LIST,
    NARROWEST,
    PALETTE_AT,
    UNSCRIPTED,
    viewport,
)
from hq.platform.core.tests.test_browser_fixtures import PAGES


class UnscriptedLayoutTests(LayoutBrowserTests):
    """Every page without script, under the whole survey: the page as the
    server drew it, where layout must hold before progressive enhancement.

    Without script nothing on a page reads the width it was loaded at, so a
    page is loaded once, at its narrowest width, and resized for the rest
    (``across``): what a rule opened it closes again (``hold_opened``).
    """

    @sweep
    def test_every_page_keeps_the_layout_rules_without_script(self):
        for name in self.mine(PAGES):
            for width in self.across(name, UNSCRIPTED.widths):
                self.hold(name, width, [self.a_heads_note_costs_its_head_no_room, self.every_layout_check_holds])
                if width in PALETTE_AT:
                    was = self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], scheme="dark")
                    self.in_scheme(was)
                self.hold_opened(name, width, popovers=width in UNSCRIPTED.popovers_at)

    def across(self, name, widths):
        """``name`` at each of ``widths``, narrowest first, from one load; a
        page that does not load fails once."""

        first, *rest = sorted(widths)
        if not self.loaded(name, first):
            return
        yield first
        for width in rest:
            self.page.set_viewport_size(viewport(width))
            # A rule the new width changes may transition; the load it stands
            # for would have drawn it finished.
            self.page.evaluate(FINISH_TRANSITIONS)
            yield width


class ScriptedLayoutTests(LayoutBrowserTests):
    """Every page once its script has run."""

    scripted = True

    @sweep
    def test_every_page_keeps_the_layout_rules_once_its_script_has_run(self):
        """Every layout check holds, at the laptop window where a wide table
        has the least room too, and the page's tables at every width from its
        first load. Script reads the width a page loads at, so each width is
        a load."""

        for name in self.mine(PAGES):
            for at, width in enumerate(DENSE.at(self.draws_a_table)):
                if not self.loaded(name, width, script="on"):
                    continue
                self.page.evaluate(SETTLE)
                if width in PALETTE_AT:
                    # Light is held by every layout check below.
                    was = self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], script="on", scheme="dark")
                    self.in_scheme(was)
                self.hold(name, width, [self.every_layout_check_holds], script="on")
                if not at:
                    self.hold(name, width, [self.a_table_fits_its_box_at_every_width], script="on")


class LongestValueLayoutTests(LayoutBrowserTests):
    """Every list in its box with the longest values its model holds."""

    scripted = True

    @sweep
    def test_every_list_fits_its_box_with_the_longest_values_its_model_holds(self):
        """Every list, its rows derived from its model's fields
        (test_browser_derived_fixtures), as it loads and with every row open."""

        held = [self.a_table_fits_its_box]
        for name in self.mine(self.lists):
            for width in LIST.at():
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
