"""A table's rows, drawn by the row tags: each row's tick-box, its state, its
toggle in the gutter, and the heading row of a group."""

from django.test import SimpleTestCase

from hq.platform.application.tests.markup import all_elements, elements, select

from .test_table_contract import _render


class TableRowTests(SimpleTestCase):
    def test_the_select_all_heading_holds_its_tick_box(self):
        html = _render('{% table_heading "Select all rows" select_all=True %}')
        self.assertIn("select-column", elements(html, "th")[0].classes)
        (box,) = select(html, "data-select-all")
        self.assertEqual(box.attrs["aria-label"], "Select all rows")

    def test_a_row_ticks_by_its_key_and_one_with_no_key_keeps_its_cell(self):
        """Each row's tick-box is labeled and sends the row's key; a row with
        none keeps an empty cell, so its columns stay under their headings."""

        source = (
            '{% table_region "Things" %}<table class="data-table" data-selectable-table><thead><tr>'
            '{% table_heading "Select all rows" select_all=True %}{% table_heading "Name" %}</tr></thead><tbody>'
            "{% for pk in keys %}<tr>{% row_select pk %}<td>thing</td></tr>{% endfor %}</tbody></table>"
            "{% endtable_region %}"
        )
        html = _render(source, {"keys": [7, None]})
        boxes = select(html, "data-row-select")
        self.assertEqual([box.attrs["value"] for box in boxes], ["7"])
        self.assertEqual(boxes[0].attrs["aria-label"], "Select row")
        self.assertEqual(len([cell for cell in elements(html, "td") if "select-column" in cell.classes]), 2)

    def test_a_row_says_it_is_emphasized_and_no_other_does(self):
        html = _render(
            "<table><tbody>{% for each in rows %}<tr{% row_state emphasized=each %}><td>x</td></tr>{% endfor %}"
            "</tbody></table>",
            {"rows": [True, False]},
        )
        self.assertEqual(["data-emphasized" in row.attrs for row in elements(html, "tr")], [True, False])

    def test_the_gutter_column_holds_each_rows_toggle(self):
        """Under a heading that keeps the gutter, each row's cell in that
        column stands in the gutter and ends with the row's toggle; a folded
        group's row keeps its own, a totals row stands in the gutter with no
        toggle, and a cell that spans the table holds none."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Select all rows" select_all=True %}{% table_heading "Name" gutter=True %}'
            '{% table_heading "Note" drop=1 %}</tr></thead><tbody>'
            '<tr><td class="select-column"></td><td class="cell-data" data-probe="classed">A <b>a</b></td><td>b</td></tr>'
            '<tr><td class="select-column"></td><th scope="row" data-probe="plain">B</th><td>c</td></tr>'
            '<tr><td class="select-column"></td><td data-probe="folds">'
            '{% include "partials/_row_fold.html" with label="C" only %}C</td><td>d</td></tr>'
            '<tr><td colspan="3" data-probe="spans">Nothing else</td></tr>'
            '</tbody><tfoot><tr><td></td><td data-probe="total">Total</td><td></td></tr></tfoot>'
            "</table>{% endtable_region %}"
        )
        found = all_elements(_render(source))
        probes = {each.attrs["data-probe"]: each for each in found if "data-probe" in each.attrs}
        self.assertEqual(
            {name: "row-gutter" in cell.classes for name, cell in probes.items()},
            {"classed": True, "plain": True, "folds": True, "spans": False, "total": True},
        )
        self.assertIn("cell-data", probes["classed"].classes)
        toggles = {
            name: [
                each.attrs.get("data-fold-toggle", "row")
                for each in found
                if cell in each.ancestors and "data-row-toggle" in each.attrs
            ]
            for name, cell in probes.items()
        }
        self.assertEqual(toggles, {"classed": ["row"], "plain": ["row"], "folds": [""], "spans": [], "total": []})
        # Last in its cell, after what names the row.
        children = [each for each in found if each.ancestors and each.ancestors[-1] is probes["classed"]]
        self.assertIn("data-row-toggle", children[-1].attrs)

    def test_a_toggle_written_in_the_gutter_is_refused(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" gutter=True %}'
            "</tr></thead><tbody><tr><td>A{% row_toggle %}</td></tr></tbody></table>{% endtable_region %}"
        )
        with self.assertRaisesMessage(ValueError, "the region's to draw"):
            _render(source)

    def test_a_groups_heading_row_spans_every_column_its_table_declares(self):
        html = _render(
            '{% table_region "Records" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" %}{% table_heading "Type" %}{% table_heading "Value" %}</tr></thead>'
            '<tbody>{% row_group "Policy" 2 %}<tr><td>a</td><td>b</td><td>c</td></tr></tbody>'
            "</table>{% endtable_region %}"
        )
        (heading,) = select(html, "scope", "rowgroup")
        self.assertEqual(heading.attrs["colspan"], "3")
        self.assertEqual(heading.words, "Policy 2")
        self.assertNotIn("data-kind", heading.attrs)

    def test_a_group_in_a_table_without_headings_spans_its_widest_row(self):
        html = _render(
            '{% table_region "Queue" %}<table class="data-table"><tbody>{% row_group "Example" 2 %}'
            '<tr><td>a</td><td>b</td><td>c</td></tr><tr><td colspan="2">a</td></tr></tbody>'
            "</table>{% endtable_region %}"
        )
        (heading,) = select(html, "scope", "rowgroup")
        self.assertEqual(heading.attrs["colspan"], "3")

    def test_a_folded_group_starts_closed_and_names_the_row_its_rows_fold_under(self):
        html = _render('{% row_group "Policy" folds="example-policy" %}')
        (row,) = elements(html, "tr")
        self.assertEqual(row.attrs["id"], "example-policy")
        self.assertIn("data-folds", row.attrs)
        (toggle,) = select(html, "data-fold-toggle")
        self.assertEqual(toggle.attrs["aria-expanded"], "false")

    def test_a_groups_name_links_where_its_rows_are_dealt_with_when_given_a_page(self):
        (link,) = elements(_render('{% row_group "Policy" 2 href="/example/policy/" %}'), "a")
        self.assertEqual((link.words, link.attrs["href"]), ("Policy", "/example/policy/"))
        self.assertFalse(elements(_render('{% row_group "Policy" 2 %}'), "a"))

    def test_a_row_toggle_starts_closed(self):
        # Drawn by hand in a table whose rows have no heading to keep a gutter.
        html = _render('<td class="row-gutter">a{% row_toggle %}</td>')
        (toggle,) = select(html, "data-row-toggle")
        self.assertEqual(toggle.attrs["aria-expanded"], "false")
        self.assertIn("row-expand", toggle.classes)
