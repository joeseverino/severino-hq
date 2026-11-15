"""The catalog under the whole survey. It lives apart from the catalog itself,
which the Design page draws on a server, so a draw of the gallery imports none
of the browser harness."""

from typing import override

from hq.platform.core.tests.browser.catalog import CATALOG, PAGES, left_out_on, page
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, SPARSE_WIDTHS

# Each drawing with no box of its own while the box that holds it has one.
_CLOSED = """(disclosure) => document.querySelector(
  `${disclosure}:not([open]), [popover]:not(:popover-open)`) !== null"""

_UNDRAWN = """(drawings) => [...document.querySelectorAll(drawings)].filter((drawing) => {
  const held = drawing.parentElement.getBoundingClientRect();
  const drawn = drawing.getBoundingClientRect();
  return held.width > 0 && held.height > 0 && (drawn.width < 1 || drawn.height < 1);
}).map((drawing) => drawing.outerHTML.slice(0, 120))"""


class CatalogLayoutTests(LayoutBrowserTests):
    """Every catalog entry under the whole survey, without script, at the
    sparse widths, which cross the stylesheet's media queries: closed, in both
    palettes, and with every disclosure open.

    The gallery is held to the same: each part there is in a shadow root of
    its own, which no neighbor reaches, so the survey judges the page around
    them, its map and each entry's name."""

    @classmethod
    @override
    def render(cls):
        return {name: page(name) for name in PAGES}

    @sweep
    def test_every_entry_keeps_the_layout_rules_without_script(self):
        for name in self.mine(PAGES):
            for width in SPARSE_WIDTHS:
                if not self.loaded(name, width):
                    continue
                self.hold(name, width, [self.draws_the_state_it_stands_for, self.every_layout_check_holds])
                # A palette is a color per token, the same at every width, so
                # the narrowest and widest read every part either draws.
                if width in (SPARSE_WIDTHS[0], SPARSE_WIDTHS[-1]):
                    was = self.in_scheme("dark")
                    self.hold(name, width, [self.every_palette_check_holds], scheme="dark")
                    self.in_scheme(was)
                # With nothing closed to open, the page open is the page as read.
                if self.page.evaluate(_CLOSED, SELECTORS["disclosure"]):
                    opened = [
                        self.open_every_disclosure,
                        self.every_layout_check_holds,
                        self.every_anchored_popover_keeps_the_layout_rules,
                    ]
                    self.hold(name, width, opened, disclosures="open")

    @sweep
    def test_every_drawing_is_drawn_on_a_phone(self):
        """A chart, a trend, a bar or a calendar is drawn wherever what holds
        it is: on a phone a drawing hidden for want of room is a figure that
        shows nothing, when the table beside it or the line under it has the
        room to show it."""

        for name in self.mine(PAGES):
            if not self.loaded(name, PHONE_SCREEN[0]):
                continue
            with self.subTest(page=name):
                self.assertEqual(self.read([(_UNDRAWN, SELECTORS["drawing"])])[0], [])

    @override
    def left_out_on(self, path):
        return left_out_on(path)

    def draws_the_state_it_stands_for(self, name):
        if name in CATALOG and (premise := CATALOG[name].premise):
            self.assertEqual(self.read([premise])[0], [], f"{name} does not draw the state it stands for")
