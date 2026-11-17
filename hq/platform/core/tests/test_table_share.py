"""A table's columns share its region: each column's share is counted over the
columns it shows, and a row's controls keep their line only up to the room
their column is sure of (docs/Design.md, A table fits its box)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import all_elements, elements


def _render(source: str) -> str:
    return Template(source).render(Context())


class TableShareTests(SimpleTestCase):
    def test_a_table_says_how_many_columns_share_its_region(self):
        """Each column's share is counted over the columns a table shows
        (tables.css): its headings less one with nothing in any row, or in a
        table with no headings its widest row."""

        headed = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "Note" %}{% table_heading "State" "status" %}</tr></thead><tbody>'
            '<tr><td>a</td><td><span class="empty-value">–</span></td><td>Up</td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        headless = (
            '{% table_region "Things" %}<table class="data-table"><tbody>'
            '<tr><td>a</td></tr><tr><td>b</td><td class="row-actions"><a class="btn" href="/example/">Open</a></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        for name, source in {"headed": headed, "headless": headless}.items():
            with self.subTest(name):
                (table,) = elements(_render(source), "table")
                self.assertEqual(table.attrs.get("data-columns"), "2")

    def test_a_rows_several_controls_are_held_in_one_box(self):
        """A row's controls keep their line up to the room their column is
        sure of and wrap between them past it (tables.css), so several are
        held in one box of the region's; a single control is its own."""

        actions = '<a class="btn" href="/example/1/">Open example</a><button class="btn">Check again</button>'
        source = (
            '{% table_region "Things" %}<table class="data-table"><tbody>'
            f'<tr><td>a</td><td class="row-actions" data-col="several">{actions}</td></tr>'
            '<tr><td>b</td><td class="row-actions" data-col="one"><a class="btn" href="/example/2/">Open</a></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        found = all_elements(_render(source))
        (box,) = [each for each in found if "cell-controls" in each.classes]
        self.assertEqual(box.ancestors[-1].attrs.get("data-col"), "several")
        self.assertEqual([each.words for each in found if box in each.ancestors], ["Open example", "Check again"])
