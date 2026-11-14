"""A block a row keeps beneath itself (`row-below`) is drawn once, on the row's
open line, which is the only place it shows (docs/Design.md, the row's line)."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import all_elements

_SOURCE = (
    '{% load table_tags %}{% table_region "Things" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Name" gutter=True %}{% table_heading "Note" %}</tr></thead><tbody>'
    '<tr data-probe="tall"><td>A <div class="row-below">tall <b>words</b></div></td>'
    '<td>Said <ul class="row-below" data-label="Also"><li>x</li></ul></td></tr>'
    '<tr data-probe="plain"><td>B</td><td>Up</td></tr>'
    "</tbody></table>{% endtable_region %}"
)


class RowBelowTests(SimpleTestCase):
    def test_a_block_kept_beneath_a_row_is_drawn_once_on_its_line(self):
        """The server moves the block to the line beneath rather than copying
        it, and marks the row (`data-below`) for the stylesheet to show its
        toggle."""

        found = all_elements(Template(_SOURCE).render(Context({})))
        blocks = [each for each in found if "row-below" in each.classes]
        self.assertEqual([each.words for each in blocks], ["tall words", "x"])
        for block in blocks:
            self.assertTrue(any("data-row-detail" in up.attrs for up in block.ancestors), block.words)
        marked = {each.attrs["data-probe"]: "data-below" in each.attrs for each in found if "data-probe" in each.attrs}
        self.assertEqual(marked, {"tall": True, "plain": False})
