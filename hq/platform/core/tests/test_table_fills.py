"""A region that fills (`table_region ... fills=True`) writes each cell of a
row once, and the bytes a row and a page with no rows draw hold to their
budgets (docs/Design.md, Opening a row moves no column)."""

import re

from django.template import Context, Template
from django.test import SimpleTestCase
from django.urls import reverse

from hq.platform.application.markup_testing import all_elements, with_class

from .signed_in_case import _AuthedTestCase

# The bytes a row of `test_a_filled_rows_bytes_hold_to_their_budget`'s table
# may draw, its cells and their marks written once.
ROW_BYTES = 1_600
# The bytes a signed-in page of a list with no rows may draw: the page's
# chrome, every frame it carries for its scripts, and the list's empty state.
CHROME_BYTES = 17_200


def _render(source: str, context: dict | None = None) -> str:
    return Template(source).render(Context(context or {}))


class FillingRegionTests(SimpleTestCase):
    def test_a_region_that_fills_writes_each_cell_once(self):
        """A region that fills draws no copy of a cell on a row's open line:
        the row's own cells are what tables.js fills it from, through the one
        frame the region draws. A row whose held cell holds a button keeps its
        copies, since a clone would be a second control, and says so."""

        source = (
            '{% table_region "Things" fills=True %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" gutter=True %}{% table_heading "Note" drop=1 %}'
            '{% table_heading "State" "status" %}{% table_heading "Act" hidden=True css="actions-col" %}'
            "</tr></thead><tbody>"
            '<tr data-probe="plain"><td>A <span class="narrow-more"><b data-probe="narrow">n</b></span>'
            '<div class="row-below" data-label="Also"><i data-probe="below">x</i></div></td>'
            '<td><i data-probe="dropped">said</i></td>'
            '<td>Up <span class="row-more"><i data-probe="more">since</i></span></td>'
            '<td><a href="/x">Open</a></td></tr>'
            '<tr data-probe="pressed"><td>B</td><td><button type="button" data-probe="press">Go</button></td>'
            "<td>Up</td><td>–</td></tr>"
            "</tbody></table>{% endtable_region %}"
        )
        html = _render(source)
        found = all_elements(html)
        probes = [each.attrs["data-probe"] for each in found if "data-probe" in each.attrs]

        for once in ("narrow", "below", "dropped", "more"):
            self.assertEqual(probes.count(once), 1, once)
        self.assertEqual(probes.count("press"), 2)
        self.assertEqual(html.count("<template data-open-line"), 1)
        # Its rows' lines are drawn only as they open, so the table says a row
        # can open, which keeps the gutter.
        (table,) = [each for each in found if each.tag == "table"]
        self.assertIn("data-rows-can-open", table.attrs)
        rows = {each.attrs["data-probe"]: each for each in found if each.tag == "tr" and "data-probe" in each.attrs}
        self.assertNotIn("data-filled", rows["plain"].attrs)
        self.assertIn("data-filled", rows["pressed"].attrs)
        self.assertEqual(with_class(html, "row-controls"), [])

    def test_a_filled_rows_bytes_hold_to_their_budget(self):
        """Bytes per row of a table like the connections list, whose rows hold
        a ranked column, a list's rest and a block beneath: a number load does
        not move, so it holds the cut of writing each cell once."""

        row = (
            '<tr id="row-{n}"><td><strong><a href="/example/{n}/">example-{n}</a></strong>'
            '<br><code>192.0.2.{n}:8080</code><span class="muted narrow-more"><br>Reached at example-host</span>'
            '<div class="row-below" data-label="Changes"><ul class="dot-list"><li>A record</li></ul></div></td>'
            '<td>{{% line_list reads as read list="dot-list" %}}<li>{{{{ read }}}} <span class="muted row-more">2</span>'
            "</li>{{% endline_list %}}</td>"
            '<td><a href="/example/host/">example-host</a></td>'
            '<td><span class="toned toned-good">Working</span><span class="muted row-more"><br>Full access</span></td>'
            '<td><a class="btn compact" href="/example/{n}/edit/">Edit</a></td></tr>'
        )
        rows = 20
        source = (
            '{% table_region "Things" fills=True %}<table class="data-table"><thead><tr>'
            '{% table_heading "Connection" "identifier" gutter=True %}{% table_heading "Reads" drop=2 %}'
            '{% table_heading "Reaches" drop=1 %}{% table_heading "State" "status" %}'
            '{% table_heading "Actions" hidden=True css="actions-col" %}</tr></thead><tbody>'
            + "".join(row.format(n=n) for n in range(rows))
            + "</tbody></table>{% endtable_region %}"
        )
        html = _render(source, {"reads": ["DNS client", "DNS lookups", "DNS server", "Internal DNS record"]})
        body = re.search(r"<tbody>.*</tbody>", html, re.DOTALL).group(0)

        self.assertLessEqual(len(body.encode()) / rows, ROW_BYTES)


class EmptyListBytesTests(_AuthedTestCase):
    def test_a_page_with_no_rows_holds_to_its_chrome_budget(self):
        """What every page carries, read where no row adds to it: a number load
        does not move, so a frame or a part added to every page is weighed."""

        page = self.client.get(reverse("expenses:list"))
        # The sections' list grows with what is composed, so it is left out.
        chrome = re.sub(r'<nav class="primary-nav".*?</nav>', "", page.content.decode(), flags=re.DOTALL)

        self.assertEqual(with_class(page, "data-table"), [])
        self.assertLessEqual(len(chrome.encode()), CHROME_BYTES)
