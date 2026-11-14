"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. A new state of a shared part is
one entry in the module of its family under ``families/``, the prefix of its
name (``part/``, ``table/``). A new family is a new module there and one
import below."""

from typing import override

from django.template.context import RenderContext
from django.template.loader import render_to_string
from django.utils.html import escape

from hq.platform.application.ui import PageNavigation, PageSection
from hq.platform.core.tests.browser.catalog.common import Entry, head_of, part_page
from hq.platform.core.tests.browser.catalog.families import chart, form, graph, part, status, table
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, SPARSE_WIDTHS


def _assembled(*families) -> dict[str, Entry]:
    """Every family's ``ENTRIES``, each in its module's own order. A family
    holds only names it begins, so no two modules name one entry."""

    catalog: dict[str, Entry] = {}
    for family in families:
        prefix = family.__name__.rpartition(".")[2]
        if strays := [name for name in family.ENTRIES if not name.startswith(f"{prefix}/")]:
            raise ImportError(f"families/{prefix}.py holds entries of another family: {strays}")
        catalog |= family.ENTRIES
    return catalog


CATALOG = _assembled(chart, form, graph, part, status, table)

# Every entry on one page (`gallery_body`), held by the survey as each entry
# is (`CatalogLayoutTests`), and the pages the catalog draws.
GALLERY = "gallery"
PAGES = (*CATALOG, GALLERY)
# The axe rules the gallery leaves out, with why, held everywhere else.
GALLERY_LEAVES_OUT = {
    "landmark-unique": "each entry is held to it on its own page; the gallery repeats parts by design",
}
# The gallery's title, as a page names itself.
GALLERY_TITLE = "Shared parts"
# The map of the gallery's families, as a page draws its own.
NAVIGATION = "partials/_page_navigation.html"


def gallery_body() -> str:
    """Every entry but the frame's own, in the catalog's order, each in a
    region named for it under its name, after a map that opens each family at
    its first entry: the gallery's content column."""

    drawn: list[str] = []
    families: list[str] = []
    # One page, so each section's id is numbered past the others' as on any page.
    page = RenderContext()
    for name, entry in CATALOG.items():
        if entry.frame:
            continue
        anchor = ""
        if (family := name.partition("/")[0]) not in families:
            families.append(family)
            anchor = f' id="design-{family}"'
        # A part that is a page's head is its own heading, so its name goes above it.
        named = head_of(name) if entry.heads_the_page else ""
        drawn.append(f'<div{anchor} role="region" aria-label="{escape(name)}">{named}{entry.drawn(name, page)}</div>')
    navigation = PageNavigation(tuple(PageSection(f"design-{family}", family) for family in families), label="Families")
    return render_to_string(NAVIGATION, {"navigation": navigation}) + "".join(drawn)


def _gallery_page() -> str:
    """The gallery on a page of its own: the document the survey judges and
    the Design page frames. The frame's parts (the site header, the demo's
    foot) hold to a page's edge, so each is judged on its page alone and the
    gallery draws none of them."""

    return part_page(GALLERY_TITLE, head_of(GALLERY_TITLE) + gallery_body())


def page(name) -> str:
    """The page ``name`` of ``PAGES`` is drawn on."""

    return _gallery_page() if name == GALLERY else CATALOG[name].page(name)


def left_out_on(name) -> tuple[str, ...]:
    """The axe rules the page ``name`` of ``PAGES`` leaves out."""

    return tuple(GALLERY_LEAVES_OUT) if name == GALLERY else ()


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

    The gallery is held to the same, so what an entry shows among the others
    is what it shows alone: each entry alone finds nothing, so a finding on the
    gallery is one a neighbor caused (a shared id, a rule scoped to a lone
    entry), and one a neighbor hid still fails the entry's own page."""

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
                    self.hold(
                        name, width, [self.open_every_disclosure, self.every_layout_check_holds], disclosures="open"
                    )

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
