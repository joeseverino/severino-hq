"""A table region writes each cell of a row once, and the bytes a row and a
page with no rows draw hold to their budgets (docs/Design.md, Opening a row
moves no column)."""

import re

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from hq.platform.application.markup_testing import all_elements, with_class

from .signed_in_case import _AuthedTestCase

# The bytes a row of `test_a_rows_bytes_hold_to_their_budget`'s table
# may draw, its cells and their marks written once.
ROW_BYTES = 1_410
# The bytes a signed-in page of a list with no rows may draw: the page's
# chrome, every frame it carries for its scripts, and the list's empty state.
CHROME_BYTES = 16_700
# Assets by their plain names: an image's collected copies are named by their
# content, which lengthens every reference by its hash wherever it is served
# from one, so the markup is weighed alike in a checkout and in an image.
_PLAIN_ASSETS = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _render(source: str, context: dict | None = None) -> str:
    return Template(source).render(Context(context or {}))


class RowWrittenOnceTests(SimpleTestCase):
    def test_a_row_writes_each_cell_once(self):
        """A region draws no copy of a cell on a row's open line: the row's
        own cells are what tables.js fills it from, through the one frame the
        region draws. A block the row moves there is drawn there alone."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" gutter=True %}{% table_heading "Note" drop=1 %}'
            '{% table_heading "State" "status" %}{% table_heading "Act" hidden=True css="actions-col" %}'
            "</tr></thead><tbody>"
            '<tr><td>A <span class="narrow-more"><b data-probe="narrow">n</b></span>'
            '<div class="row-below" data-label="Also"><i data-probe="below">x</i></div></td>'
            '<td><i data-probe="dropped">said</i></td>'
            '<td>Up <span class="row-more"><i data-probe="more">since</i></span></td>'
            '<td><a href="/x" data-probe="act">Open</a></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        html = _render(source)
        found = all_elements(html)
        probes = [each.attrs["data-probe"] for each in found if "data-probe" in each.attrs]

        self.assertEqual(sorted(probes), ["act", "below", "dropped", "more", "narrow"])
        self.assertEqual(html.count("<template data-open-line"), 1)
        # Its rows' lines are drawn only as they open, so the table says a row
        # can open, which keeps the gutter.
        (table,) = [each for each in found if each.tag == "table"]
        self.assertIn("data-rows-can-open", table.attrs)
        (below,) = [each for each in found if each.attrs.get("data-probe") == "below"]
        self.assertTrue(any("row-detail" in up.classes for up in below.ancestors))

    def test_a_cell_held_back_holds_no_control(self):
        """A cell its row's open line copies holds no button or field, which
        the copy would draw twice: it stands in place, or moves there whole."""

        held = {
            "a ranked column": '<td><button type="button">Go</button></td>',
            "a part only the open line shows": '<td>Up <span class="row-more"><select><option>a</option></select></span></td>',
        }
        for name, cell in held.items():
            with self.subTest(name):
                source = (
                    '{% table_region "Things" %}<table class="data-table"><thead><tr>'
                    '{% table_heading "Name" %}{% table_heading "Note" drop=1 %}</tr></thead><tbody>'
                    f"<tr><td>A</td>{cell}</tr></tbody></table>{{% endtable_region %}}"
                )
                with self.assertRaisesMessage(ValueError, "holds no control"):
                    _render(source)

    def test_a_rows_bytes_hold_to_their_budget(self):
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
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Connection" "identifier" gutter=True %}{% table_heading "Reads" drop=2 %}'
            '{% table_heading "Reaches" drop=1 %}{% table_heading "State" "status" %}'
            '{% table_heading "Actions" hidden=True css="actions-col" %}</tr></thead><tbody>'
            + "".join(row.format(n=n) for n in range(rows))
            + "</tbody></table>{% endtable_region %}"
        )
        html = _render(source, {"reads": ["DNS client", "DNS lookups", "DNS server", "Internal DNS record"]})
        body = re.search(r"<tbody>.*</tbody>", html, re.DOTALL).group(0)

        self.assertLessEqual(len(body.encode()) / rows, ROW_BYTES)


@override_settings(STORAGES=_PLAIN_ASSETS)
class EmptyListBytesTests(_AuthedTestCase):
    def test_a_page_with_no_rows_holds_to_its_chrome_budget(self):
        """What every page carries, read where no row adds to it: a number load
        does not move, so a frame or a part added to every page is weighed."""

        page = self.client.get(reverse("expenses:list"))
        # The sections' list grows with what is composed, so it is left out.
        chrome = re.sub(r'<nav class="primary-nav".*?</nav>', "", page.content.decode(), flags=re.DOTALL)

        self.assertEqual(with_class(page, "data-table"), [])
        self.assertLessEqual(len(chrome.encode()), CHROME_BYTES)
