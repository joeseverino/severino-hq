"""The catalog under the whole survey. It lives apart from the catalog itself,
which the Design page draws on a server, so a draw of the gallery imports none
of the browser harness."""

from typing import override

from hq.platform.core.tests.browser.catalog import CATALOG, PAGES, left_out_on, page
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.widths import ENTRY, SCRIPTED_ENTRY


class EntryLayoutTests(LayoutBrowserTests):
    """Every catalog entry under the whole survey, in one of the two states
    its page is read in (`widths.ENTRY`, `widths.SCRIPTED_ENTRY`): as the
    server drew it, and once HQ's scripts have run, which every entry's page
    loads as base.html does.

    The gallery is held to the same: each part there is in a shadow root of
    its own, which no neighbor reaches, so the survey judges the page around
    them, its map and each entry's name."""

    @classmethod
    @override
    def render(cls):
        return {name: page(name) for name in PAGES}

    def sweep(self, judged, **where):
        """Every page at each width ``judged`` reads: closed, with every fold
        (and with script, every row) open, its tables at every width from its
        first load where ``judged`` asks, and without script in both palettes."""

        for name in self.mine(PAGES):
            for at, width in enumerate(judged.at(self.draws_a_table)):
                if not self.loaded(name, width, **where):
                    continue
                self.settle()
                self.hold(name, width, [self.draws_the_state_it_stands_for, self.every_layout_check_holds], **where)
                # A palette is a color per token, the same at every width, so
                # the narrowest and widest read every part either draws.
                if not self.runs_script and width in (judged.widths[0], judged.widths[-1]):
                    was = self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], scheme="dark")
                    self.in_scheme(was)
                self.hold_opened(name, width, popovers=width in judged.popovers_at, **where)
                if judged.every_width and not at:
                    self.hold(name, width, [self.a_table_fits_its_box_at_every_width], **where)

    @override
    def left_out_on(self, path):
        return left_out_on(path)

    def draws_the_state_it_stands_for(self, name):
        if name in CATALOG and (premise := CATALOG[name].premise):
            self.assertEqual(self.read([premise])[0], [], f"{name} does not draw the state it stands for")


class CatalogLayoutTests(EntryLayoutTests):
    @sweep
    def test_every_entry_keeps_the_layout_rules_without_script(self):
        self.sweep(ENTRY)


class ScriptedCatalogLayoutTests(EntryLayoutTests):
    scripted = True

    @sweep
    def test_every_entry_keeps_the_layout_rules_once_its_script_has_run(self):
        self.sweep(SCRIPTED_ENTRY, script="on")
