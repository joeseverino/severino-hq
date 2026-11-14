"""The sweep of every table at every shape of the layout, in each state a
reader can draw its rows in: over the example estate, and proven able to fail
on a defect of each kind it reads that only a width no other gate samples
shows."""

import json
import re
from typing import Any, override

from hq.platform.core.stylesheets import stylesheet
from hq.platform.core.tests.browser.estate import ExampleEstateGate
from hq.platform.core.tests.browser.gate import BrowserGate, add_css
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.shards import sweep
from hq.platform.core.tests.browser.widths import (
    LARGEST,
    NARROWEST,
    SPARSE_WIDTHS,
    WIDE_TABLES,
    WIDTHS,
    query_edges,
    viewport_queries,
)


class ExampleEstateTableFitTests(ExampleEstateGate):
    """Every table of the example estate keeps the table checks at every width."""

    @sweep
    def test_every_table_keeps_every_table_check_at_every_width(self):
        """From the narrowest phone up, with every disclosure open, its rows
        as drawn and with every row a reader can open opened: a width the
        layout sweep skips is where a table ran past its card, or a cell past
        its column, unseen."""

        self.assertTrue(self.navigation, "the navigation led nowhere")
        for path in self.mine(self.navigation):
            with self.subTest(page=path):
                self.visit(path, NARROWEST)
                self.page.evaluate(
                    "(disclosure) => document.querySelectorAll(disclosure).forEach((d) => { d.open = true; })",
                    SELECTORS["disclosure"],
                )
                found = self.table_defects_at_every_width()
                if found:
                    self.fail(json.dumps(found, indent=1))


def unsampled_span():
    """The first span between two of the stylesheet's viewport-query edges
    that holds none of the widths the other gates read a page at."""

    sampled = {*SPARSE_WIDTHS, *WIDTHS, *WIDE_TABLES}
    edges = query_edges(viewport_queries(stylesheet()))
    for low, high in zip(edges, [*(edge - 1 for edge in edges[1:]), LARGEST], strict=True):
        if not any(low <= width <= high for width in sampled):
            return low, high
    raise AssertionError(f"every span between the stylesheet's edges {edges} holds a sampled width")


# A table whose first row opens (a list it folds keeps its toggle at every
# width) and whose second has nothing to open, a table with no gutter, whose
# row cannot open, and a table with a column that leaves first, a plant in each
# that reads one: ``opened`` in the row that opens, ``closed`` in the one that
# cannot, ``ranked`` beside the column that leaves.
_TABLE = """{% table_region "Planted" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Tags" %}</tr></thead><tbody>
<tr><td>Opens</td><td>OPENED</td>
<td><span class="tag">One</span><span class="muted row-less">+1</span><span class="row-more"><span class="tag">Two</span></span></td></tr>
<tr><td>Stays</td><td>Example</td><td><span class="tag">One</span></td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Fixed" %}<table class="data-table"><thead><tr>
{% table_heading "Name" %}{% table_heading "Value" %}{% table_heading "Tags" %}</tr></thead><tbody>
<tr><td>Fixed</td><td>CLOSED</td><td><span class="tag">One</span></td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Ranked" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Note" drop=1 %}</tr></thead><tbody>
<tr><td>Ranked</td><td>RANKED</td><td><span class="plant-wide">Example</span></td></tr>
</tbody></table>{% endtable_region %}"""

# A host name drawn on two lines, which `broken identifiers` reads in a closed
# row only.
_BROKEN_HOST = (
    '<span style="display: block; inline-size: var(--plant, auto)"><span class="token" data-sole '
    'style="white-space: normal">status.<wbr>example.com</span></span>'
)

# What an open row lifts (surfaces.css) it lifts with `!important`, which only
# a box's own style outranks; each plant's measure is a property that the
# span's query sets, so the defect is drawn at no width outside it. A plant
# named other than its check says which (``check``); one whose sheet needs
# widths inside the span is a function of the widths a third and two thirds
# into it.
PLANTS: dict[str, dict[str, Any]] = {
    # A box pushed past its cell's edge.
    "cells": {"opened": '<span style="position: relative; inset-inline-start: var(--plant, 0)">Example</span>'},
    # A word broken across lines though its line holds it.
    "broken words": {
        "opened": '<span style="display: block; inline-size: 9ch; text-indent: var(--plant, 0) !important; '
        'word-break: break-all !important">Example</span>'
    },
    # A host name drawn on two lines, in a row that does not open.
    "broken identifiers": {"closed": _BROKEN_HOST},
    # The same in a row that opens, drawn while it is closed; outside the span
    # it is held at its own width, since that row's column is narrow enough at
    # a phone's width to break it unheld.
    "broken identifiers in a row that opens": {
        "check": "broken identifiers",
        "opened": _BROKEN_HOST.replace("var(--plant, auto)", "var(--plant, max-content)"),
    },
    # The same only while a table is whole and short of room: from the width
    # its column comes back (its note, too wide for it below there, narrows)
    # to the span's second third, never at either end of the span.
    "broken identifiers once a column comes back": {
        "check": "broken identifiers",
        "ranked": _BROKEN_HOST,
        "css": lambda first, second: (
            f"@media (max-width: {first - 1}px) {{ main .data-table > tbody > tr:not(.row-detail) > td > .plant-wide "
            "{ display: inline-block; inline-size: 100vw; } } "
            f"@media (max-width: {second}px) {{ main .data-table:not(:has([data-dropped])) {{ --plant: 5ch; }} }}"
        ),
    },
    "tables": {"css": "main .data-table tr.is-expanded { display: block; }"},
    # The region narrowed under a table held at its width while a row is open.
    "tables fit": {"css": "main .table-scroll:has(tr.is-expanded) { inline-size: 6rem; }"},
    # A heading hidden while its cells still take the table's width.
    "unseen columns": {"css": "main .data-table:has(tr.is-expanded) > thead th:last-child { display: none; }"},
    # A box narrower than the words it holds on one line.
    "cell labels": {
        "opened": '<span style="display: inline-block; inline-size: var(--plant, auto) !important; '
        'min-inline-size: 0 !important; white-space: nowrap !important">Example label</span>'
    },
    # A short name cut in a table with room to spare.
    "cut with room": {
        "closed": '<span class="token" data-sole style="display: inline-block; max-inline-size: var(--plant, none); '
        'overflow: hidden; white-space: nowrap">example-name</span>'
    },
    # A value cut short of the room its cell holds, in a row that does not open.
    "cut beside room": {
        "closed": '<span title="Example value" style="display: block; max-inline-size: var(--plant, none); '
        'overflow: hidden; text-overflow: ellipsis; white-space: nowrap">Example value</span>'
    },
    # A value cut with no ellipsis, in a row that does not open.
    "cut and closed": {
        "closed": '<span style="display: block; max-inline-size: var(--plant, none); overflow: hidden; '
        'white-space: nowrap">Example</span>'
    },
    # A value cut in a row that is open.
    "cut when open": {
        "opened": '<span style="display: block; max-inline-size: var(--plant, 100%) !important; '
        'overflow: hidden !important; white-space: nowrap !important">Example</span>'
    },
    # The row that stayed closed grown taller by another's opening.
    "opening rows moves": {
        "css": "main .data-table:has(tr.is-expanded) > tbody > tr:not(.is-expanded, .row-detail) > td "
        "{ padding-block: 2rem; }"
    },
}

# The measure each plant's markup reads, set inside the span's query.
_MEASURES = {
    "cells": "30rem",
    "broken words": "5ch",
    "broken identifiers": "5ch",
    "broken identifiers in a row that opens": "5ch",
    "cell labels": "2rem",
    "cut with room": "3ch",
    "cut beside room": "3ch",
    "cut and closed": "3ch",
    "cut when open": "3ch",
}

_AT = re.compile(r" at (\d+)px(?:, its \w+ open)?$")


def planted_page(plant):
    page = _TABLE
    for slot in ("opened", "closed", "ranked"):
        page = page.replace(slot.upper(), plant.get(slot, "Example"))
    return probe(page)


class TableSweepPlantedTests(BrowserGate):
    """Each kind of table defect the sweep reads, planted in a span of widths
    between two of the stylesheet's edges that no other gate reads, is found
    there, in the state of its rows the kind reads and the state of its table
    the plant draws it in."""

    scripted = True

    @classmethod
    @override
    def render(cls):
        pages = {f"plant-{index}": planted_page(plant) for index, plant in enumerate(PLANTS.values())}
        return {**pages, "plant-none": planted_page({})}

    def test_a_table_with_nothing_planted_keeps_every_table_check(self):
        self.open("plant-none", NARROWEST)
        self.assertEqual(self.table_defects_at_every_width(), {})

    def test_each_defect_drawn_only_between_two_edges_is_found_there(self):
        low, high = unsampled_span()
        thirds = (low + (high - low) // 3, low + 2 * (high - low) // 3)
        for index, (name, plant) in enumerate(PLANTS.items()):
            check = plant.get("check", name)
            with self.subTest(plant=name):
                self.open(f"plant-{index}", NARROWEST)
                css = plant.get("css", "")
                said = (css(*thirds) if callable(css) else css) + (
                    f"main {{ --plant: {_MEASURES[name]}; }}" if name in _MEASURES else ""
                )
                add_css(self.page, f"@media (min-width: {low}px) and (max-width: {high}px) {{ {said} }}")
                found = self.table_defects_at_every_width()
                self.assertIn(check, found, found)
                widths = {int(width) for line in found[check] for width in _AT.findall(line)}
                self.assertTrue(widths and all(low <= width <= high for width in widths), (low, high, found[check]))
