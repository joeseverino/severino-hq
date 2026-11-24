"""A declared table's rows: each row's tick-box, its state, its toggle in
the gutter, and the heading row of a group."""

from django.test import SimpleTestCase

from hq.platform.application.markup_testing import all_elements, elements, select
from hq.platform.application.tables import Cell, DataTable, Group, Row, TableColumn

from .test_table_contract import _drawn, _render, _row, _things


def _grouped(*groups: Group) -> str:
    return _drawn(_things(("Name",), groups=groups))


class TableRowTests(SimpleTestCase):
    def test_the_select_all_heading_holds_its_tick_box(self):
        html = _drawn(_things(("Name",), rows=(_row("thing", key=7),), selectable=True))
        self.assertIn("select-column", elements(html, "th")[0].classes)
        (box,) = select(html, "data-select-all")
        self.assertEqual(box.attrs["aria-label"], "Select all rows")

    def test_a_row_ticks_by_its_key_and_one_with_no_key_keeps_its_cell(self):
        """Each row's tick-box is labeled and sends the row's key; a row with
        none keeps an empty cell, so its columns stay under their headings."""

        html = _drawn(_things(("Name",), rows=(_row("thing", key=7), _row("thing")), selectable=True))
        boxes = select(html, "data-row-select")
        self.assertEqual([box.attrs["value"] for box in boxes], ["7"])
        self.assertEqual(boxes[0].attrs["aria-label"], "Select row")
        self.assertEqual(len([cell for cell in elements(html, "td") if "select-column" in cell.classes]), 2)

    def test_a_row_says_it_is_emphasized_and_no_other_does(self):
        html = _drawn(_things(("Name",), rows=(_row("a", emphasized=True), _row("b"))))
        rows = [row for row in elements(html, "tr") if any(up.tag == "tbody" for up in row.ancestors)]
        self.assertEqual(["data-emphasized" in row.attrs for row in rows], [True, False])

    def test_the_column_that_names_each_row_draws_its_name_whether_or_not_it_links(self):
        """A row's name is the value of the column that names it, words or a
        link to its record's page, though no cell declares it: a link elsewhere
        in the row, and an identifier drawn as code, name nothing."""

        rows = (
            _row(Cell("example-run", "/example/run/"), Cell("example-host", "/example/host/"), key=1),
            _row("example-job", "example-host", key=2),
            _row(Cell("example-id", readout=True), "example-host", key=3),
        )
        html = _drawn(_things(("Work",), ("Host",), rows=rows, selectable=True))
        names = [each for each in all_elements(html) if each.attrs.get("data-cell") == "name"]
        self.assertEqual([each.words for each in names], ["example-run", "example-job"])
        self.assertEqual(names[0].ancestors[-1].attrs.get("href"), "/example/run/")

    def test_the_gutter_column_holds_each_rows_toggle(self):
        """In a table whose rows can hold something back (here a ranked
        column), each row's cell in the column that names it stands in the
        gutter and ends with the row's toggle; a folded group's row keeps its
        own, and a totals row and a cell that spans the table stand in the
        gutter with no toggle."""

        rows = (
            _row(Cell("A", parts=(Cell("a", name=True),)), "b", key=1),
            _row(Cell("B", header=True), "c", key=2),
            _row(Cell("C", fold=True), "d", key=3, folds="example-c"),
            _row("Nothing else", across=True),
        )
        html = _drawn(
            _things(("Name",), TableColumn("Note", drop=1), rows=rows, selectable=True, footer=_row("Total", ""))
        )
        found = all_elements(html)
        named = {
            "parted": next(each for each in found if each.tag == "td" and each.words.startswith("A")),
            "header": next(each for each in found if each.tag == "th" and each.words.startswith("B")),
            "folds": next(each for each in found if each.tag == "td" and each.words.startswith("C")),
            "spans": next(each for each in found if each.words == "Nothing else" and each.tag == "td"),
            "total": next(each for each in found if each.tag == "td" and each.words == "Total"),
        }
        self.assertEqual(
            {name: "row-gutter" in cell.classes for name, cell in named.items()},
            {"parted": True, "header": True, "folds": True, "spans": True, "total": True},
        )
        toggles = {
            name: [
                each.attrs.get("data-fold-toggle", "row")
                for each in found
                if cell in each.ancestors and "data-row-toggle" in each.attrs
            ]
            for name, cell in named.items()
        }
        self.assertEqual(toggles, {"parted": ["row"], "header": ["row"], "folds": [""], "spans": [], "total": []})
        # Last in its cell, after what names the row.
        children = [each for each in found if each.ancestors and each.ancestors[-1] is named["parted"]]
        self.assertIn("data-row-toggle", children[-1].attrs)

    def test_a_groups_heading_row_spans_every_column_its_table_declares(self):
        groups = (Group("Policy", (_row("a", "b", "c"),), count=2),)
        html = _drawn(_things(("Name",), ("Type",), ("Value",), groups=groups))
        (heading,) = select(html, "scope", "rowgroup")
        self.assertEqual(heading.attrs["colspan"], "3")
        self.assertEqual(heading.words, "Policy 2")
        self.assertNotIn("data-kind", heading.attrs)

    def test_a_group_in_a_table_without_headings_spans_its_widest_row(self):
        groups = (Group("Example", (_row("a", "b", "c"), _row("a", across=True)), count=2),)
        (heading,) = select(_drawn(_things(("",), ("",), ("",), groups=groups, headed=False)), "scope", "rowgroup")
        self.assertEqual(heading.attrs["colspan"], "3")

    def test_a_folded_group_starts_closed_and_names_the_row_its_rows_fold_under(self):
        html = _grouped(Group("Policy", (_row("a"),), folds="example-policy"))
        (row,) = [each for each in elements(html, "tr") if "row-group" in each.classes]
        self.assertEqual(row.attrs["id"], "example-policy")
        self.assertIn("data-folds", row.attrs)
        (toggle,) = select(html, "data-fold-toggle")
        self.assertEqual(toggle.attrs["aria-expanded"], "false")

    def test_a_groups_name_links_where_its_rows_are_dealt_with_when_given_a_page(self):
        (link,) = elements(_grouped(Group("Policy", (_row("a"),), count=2, href="/example/policy/")), "a")
        self.assertEqual((link.words, link.attrs["href"]), ("Policy", "/example/policy/"))
        self.assertFalse(elements(_grouped(Group("Policy", (_row("a"),), count=2)), "a"))

    def test_a_group_a_reader_should_weigh_first_is_marked_for_attention(self):
        weighed = _grouped(Group("Policy", (_row("a"),), attention=True), Group("Plain", (_row("b"),)))
        weighed_row, plain = [each for each in elements(weighed, "tr") if "row-group" in each.classes]
        self.assertIn("data-attention", weighed_row.attrs)
        self.assertNotIn("data-attention", plain.attrs)

    def test_a_cell_that_heads_its_row_is_its_rows_header(self):
        table = DataTable(
            "Things", (TableColumn("Name"), TableColumn("Size")), (Row((Cell("a", header=True), Cell("1"))),)
        )
        (header,) = [
            cell for cell in elements(_render("{% data_table table %}", {"table": table}), "th") if cell.words == "a"
        ]
        self.assertEqual(header.attrs.get("scope"), "row")

    def test_a_table_its_template_does_not_hold_is_refused_as_the_template_writes_it(self):
        with self.assertRaises(TypeError) as refused:
            _render("{% data_table missing %}")
        self.assertIn("{% data_table missing %}", " ".join(refused.exception.__notes__))

    def test_a_declared_hook_stands_on_what_it_finds(self):
        """A group's hook is on its body, a line's on its item, and a value's
        around that value alone, so a script reads its words and no others."""

        from hq.platform.core.part_examples.tags import DATA_TABLE_STATES

        html = _render("{% data_table table %}", DATA_TABLE_STATES["kinds"].context())
        (group,) = select(html, "data-example", "group")
        (line,) = select(html, "data-example", "zone")
        (value,) = select(html, "data-example", "digits")
        self.assertEqual((group.tag, group.attrs.get("id")), ("tbody", "example-group"))
        self.assertEqual((line.tag, line.words), ("li", "example-zone"))
        self.assertEqual(value.words, "…1234 ending in 1234")
