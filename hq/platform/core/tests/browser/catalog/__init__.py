"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. A new state of a shared part is
one entry in the module of its family under ``families/``, the prefix of its
name (``part/``, ``table/``). A new family is a new module there, one import
below and one line of ``MODULES`` in test_browser_probes."""

from typing import override

from hq.platform.core.tests.browser.catalog.common import Entry
from hq.platform.core.tests.browser.catalog.families import chart, form, graph, part, status, table
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.selectors import SELECTORS
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


# Each drawing with no box of its own while the box that holds it has one.
_UNDRAWN = """(drawings) => [...document.querySelectorAll(drawings)].filter((drawing) => {
  const held = drawing.parentElement.getBoundingClientRect();
  const drawn = drawing.getBoundingClientRect();
  return held.width > 0 && held.height > 0 && (drawn.width < 1 || drawn.height < 1);
}).map((drawing) => drawing.outerHTML.slice(0, 120))"""


class CatalogLayoutTests(LayoutBrowserTests):
    """Every catalog entry under the whole survey, without script, at the
    sparse widths, which cross the stylesheet's media queries: closed, in both
    palettes, and with every disclosure open."""

    @classmethod
    @override
    def render(cls):
        return {name: entry.page(name) for name, entry in CATALOG.items()}

    def test_every_entry_keeps_the_layout_rules_without_script(self):
        for name in CATALOG:
            for width in SPARSE_WIDTHS:
                if not self.loaded(name, width):
                    continue
                self.hold(name, width, [self.draws_the_state_it_stands_for, self.every_layout_check_holds])
                self.in_scheme("dark")
                self.hold(name, width, [self.every_palette_check_holds], scheme="dark")
                self.in_scheme("light")
                self.hold(name, width, [self.open_every_disclosure, self.every_layout_check_holds], disclosures="open")

    def test_every_drawing_is_drawn_on_a_phone(self):
        """A chart, a trend, a bar or a calendar is drawn wherever what holds
        it is: on a phone a drawing hidden for want of room is a figure that
        shows nothing, when the table beside it or the line under it has the
        room to show it."""

        for name in CATALOG:
            if not self.loaded(name, PHONE_SCREEN[0]):
                continue
            with self.subTest(page=name):
                self.assertEqual(self.read([(_UNDRAWN, SELECTORS["drawing"])])[0], [])

    def draws_the_state_it_stands_for(self, name):
        if premise := CATALOG[name].premise:
            self.assertEqual(self.read([premise])[0], [], f"{name} does not draw the state it stands for")
