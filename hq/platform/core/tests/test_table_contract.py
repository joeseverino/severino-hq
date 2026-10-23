"""A data table is declared through the table tags, so its shape is in the HTML on first paint."""

import re
from pathlib import Path

from django.template import Context, RequestContext, Template, TemplateSyntaxError, engines
from django.template.base import TextNode
from django.template.defaulttags import IfNode
from django.test import RequestFactory, SimpleTestCase, tag
from django.urls import reverse

from hq.platform.application.routes import routed
from hq.platform.application.tables import (
    DROP_STEPS,
    FitColumn,
    TableColumn,
    TableListMixin,
    column_floor,
    drop_step,
    fit,
    named_first,
    ranked,
    typed,
)
from hq.platform.application.tests.markup import all_elements, elements, select, with_class, within
from hq.platform.core.audit_views import AuditLogListView
from hq.platform.core.stylesheets import stylesheet as whole_stylesheet
from hq.platform.core.templatetags import table_tags

from .test_web import _AuthedTestCase

TEMPLATES = Path(__file__).resolve().parents[4] / "templates"

_OPEN, _CLOSE = "\x00region\x00", "\x00/region\x00"
_HEADING, _DROPPED_HEADING = "\x00heading\x00", "\x00heading-drops\x00"
_TABLE = re.compile(r'<table\b[^>]*\bclass="[^"]*\bdata-table\b')
_HEAD = re.compile(r"<thead\b.*?</thead>", re.DOTALL)
_HAND_HEADING = re.compile(r"<th[\s>]")
# A list's columns: the name, a column kept at every width, and one that may leave.
_LIST_COLUMNS = (TableColumn("Name"), TableColumn("Note", css="key-col"), TableColumn("When"))


def _func(node) -> object:
    return getattr(node, "func", None)


def _heading_marker(node) -> str:
    """A heading, marked when it is given a literal drop rank."""

    drop = node.kwargs.get("drop")
    literal = getattr(getattr(drop, "var", None), "literal", None)
    return _DROPPED_HEADING if literal else _HEADING


def _flatten(nodelist, out: list[str]) -> list[str]:
    """The template's markup in source order, every branch included, with the
    table tags standing as markers."""

    for node in nodelist:
        if isinstance(node, TextNode):
            out.append(node.s)
        elif isinstance(node, table_tags.RegionNode):
            out.append(_OPEN)
            _flatten(node.nodelist, out)
            out.append(_CLOSE)
        elif _func(node) is table_tags.table_heading:
            out.append(_HEADING if "select_all" in node.kwargs else _heading_marker(node))
        elif isinstance(node, IfNode):
            for _condition, branch in node.conditions_nodelists:
                _flatten(branch, out)
        else:
            for attribute in node.child_nodelists:
                _flatten(getattr(node, attribute, None) or (), out)
    return out


def table_problems(source: str) -> list[str]:
    """What keeps a template's tables off the contract: a table outside its region,
    a heading written by hand, a region written by hand, a dropped name column."""

    markup = "".join(_flatten(engines["django"].from_string(source).template.nodelist, []))
    problems = []
    if re.search(r'class="[^"]*\btable-scroll\b', markup):
        problems.append("a scroll region written by hand")
    for table in _TABLE.finditer(markup):
        before = markup[: table.start()]
        if before.count(_OPEN) <= before.count(_CLOSE):
            problems.append("a data table outside {% table_region %}")
        body = markup[table.start() : markup.find("</table>", table.start())]
        for head in _HEAD.findall(body):
            if _HAND_HEADING.search(head):
                problems.append("a heading not drawn by {% table_heading %}")
            first = re.search(f"{_HEADING}|{_DROPPED_HEADING}", head)
            if first and first.group() == _DROPPED_HEADING:
                problems.append("a drop rank on the column that names each row")
    return problems


def _templates() -> dict[str, str]:
    return {
        path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATES.rglob("*.html"))
    }


@tag("source")
class TableContractTests(SimpleTestCase):
    maxDiff = None

    def test_every_table_is_declared_through_the_tags(self):
        problems = {name: found for name, source in _templates().items() if (found := table_problems(source))}
        self.assertEqual(problems, {}, "Draw these tables with {% table_region %} and {% table_heading %}.")

    def test_a_hand_drawn_table_is_refused(self):
        bare = '<table class="data-table"><thead><tr><th>Name</th></tr></thead></table>'
        self.assertIn("a data table outside {% table_region %}", table_problems(bare))
        wrapped = f'<div class="table-scroll">{bare}</div>'
        self.assertIn("a scroll region written by hand", table_problems(wrapped))
        in_region = '{% table_region "Things" %}' + bare + "{% endtable_region %}"
        self.assertEqual(table_problems(in_region), ["a heading not drawn by {% table_heading %}"])

    def test_a_declared_table_keeps_the_contract(self):
        source = (
            '{% table_region "Things" %}<table class="compact data-table"><thead><tr>'
            '{% table_heading "Name" "identifier" %}{% if x %}{% table_heading "Note" drop=1 %}{% endif %}'
            '</tr></thead><tbody><tr><th scope="row">a</th><td>b</td></tr></tbody></table>{% endtable_region %}'
        )
        self.assertEqual(table_problems(source), [])

    def test_the_name_column_never_drops(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" drop=1 %}</tr></thead></table>{% endtable_region %}'
        )
        self.assertEqual(table_problems(source), ["a drop rank on the column that names each row"])

    def test_the_gutter_reads_nothing_a_script_marks(self):
        """The gutter moves every name in its column, so whether it is kept
        follows from the server's markup and the box's width, never from what
        tables.js marks after paint."""

        root = TEMPLATES.parent
        script = (root / "static" / "js" / "tables.js").read_text(encoding="utf-8")
        marked = {
            re.sub(r"[A-Z]", lambda upper: "-" + upper.group().lower(), key)
            for key in re.findall(r"dataset\.(\w+)\s*=", script)
        }
        marked |= set(re.findall(r'toggleAttribute\("data-([\w-]+)"', script))
        self.assertIn("cut", marked)
        stylesheet = re.sub(r"/\*.*?\*/", " ", whole_stylesheet(), flags=re.DOTALL)
        gutters = [
            selector
            for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", stylesheet)
            if "var(--row-gutter)" in block
        ]
        self.assertTrue(gutters)
        read = {(name, selector.strip()) for selector in gutters for name in marked if f"data-{name}" in selector}
        self.assertEqual(read, set())


def _render(source: str, context: dict | None = None, request=None) -> str:
    template = Template(source)
    if request is not None:
        return template.render(RequestContext(request, context or {}))
    return template.render(Context(context or {}))


class TableTagTests(SimpleTestCase):
    def test_a_region_is_labeled_and_only_one_that_scrolls_takes_focus(self):
        html = _render('{% table_region "Machines" %}<table class="data-table"></table>{% endtable_region %}')
        (region,) = select(html, "role", "region")
        self.assertEqual(region.attrs["aria-label"], "Machines")
        self.assertNotIn("tabindex", region.attrs)
        self.assertIn("table-scroll", region.classes)
        self.assertEqual(len(elements(html, "table")), 1)
        for source in ('{% table_region "Links" pane=True %}', '{% table_region "Runs" chart=True %}'):
            with self.subTest(source):
                (scrolls,) = select(_render(source + "{% endtable_region %}"), "role", "region")
                self.assertEqual(scrolls.attrs["tabindex"], "0")
        (pane,) = select(_render('{% table_region "Links" pane=True %}{% endtable_region %}'), "role", "region")
        self.assertIn("is-pane", pane.classes)

    def test_a_region_names_the_room_its_identifiers_get_and_still_does_not_scroll(self):
        for room in ("shared", "snug"):
            with self.subTest(room):
                source = '{% table_region "Services" room="' + room + '" %}{% endtable_region %}'
                (region,) = select(_render(source), "role", "region")
                self.assertIn(f"is-{room}", region.classes)
                self.assertNotIn("tabindex", region.attrs)
        (plain,) = select(_render('{% table_region "Services" %}{% endtable_region %}'), "role", "region")
        self.assertFalse({"is-shared", "is-snug"} & set(plain.classes))

    def test_a_room_no_share_is_named_for_is_refused(self):
        with self.assertRaises(ValueError):
            _render('{% table_region "Services" room="wide" %}{% endtable_region %}')

    def test_a_chart_region_says_so(self):
        html = _render('{% table_region "Runs" chart=True %}{% endtable_region %}')
        self.assertIn("data-chart", select(html, "role", "region")[0].attrs)

    def test_a_heading_declares_its_kind(self):
        (heading,) = elements(_render('{% table_heading "Cost" css="num-col" %}'), "th")
        self.assertEqual(heading.attrs["data-kind"], "numeric")
        (named,) = elements(_render('{% table_heading "Name" "identifier" %}'), "th")
        self.assertEqual(named.attrs["data-kind"], "identifier")

    def test_a_heading_carries_its_rank(self):
        source = (
            '{% table_heading "Name" %}{% table_heading "A" drop=1 %}{% table_heading "B" drop=3 %}'
            '{% table_heading "D" css="key-col" %}'
        )
        ranks = {heading.text.strip(): heading.attrs.get("data-drop") for heading in elements(_render(source), "th")}
        self.assertEqual(ranks, {"Name": None, "A": "1", "B": "3", "D": None})

    def test_a_regions_counts_are_accepted_and_change_nothing(self):
        table = (
            '<table class="data-table"><thead><tr>{% table_heading "Name" %}{% table_heading "Note" drop=1 %}'
            "</tr></thead><tbody><tr><td>a</td><td>b</td></tr></tbody></table>{% endtable_region %}"
        )
        counted = _render('{% table_region "Things" mid=3 narrow=1 %}' + table)
        self.assertEqual(counted, _render('{% table_region "Things" %}' + table))

    def test_a_region_refuses_what_it_does_not_take(self):
        for source in (
            "{% table_region %}{% endtable_region %}",
            '{% table_region "Things" wide=2 %}{% endtable_region %}',
        ):
            with self.subTest(source), self.assertRaises(TemplateSyntaxError):
                Template(source)

    def test_a_cell_that_says_nothing_is_marked_blank(self):
        cells = {
            "dash": "<td>\u2014</td>",
            "empty": "<td> </td>",
            "empty value": '<td><span class="empty-value">\u2013</span></td>',
            "words": "<td>Served</td>",
            "drawing": '<td><svg aria-hidden="true"></svg></td>',
            "control": "<td><button>Go</button></td>",
            "line break": "<td><br></td>",
        }
        rows = "".join(f'<tr><th scope="row">{name}</th>{cell}</tr>' for name, cell in cells.items())
        nested = "<tr><th>outer</th><td><table><tbody><tr><td>\u2014</td></tr></tbody></table></td></tr>"
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr><th>Name</th><th>Value</th></tr></thead>'
            "<tbody>" + rows + nested + "</tbody></table>{% endtable_region %}"
        )
        found = all_elements(_render(source))
        blank = [cell for cell in found if cell.tag == "td" and "data-blank" in cell.attrs]
        marked = {
            each.text.strip()
            for each in found
            if each.tag == "th" and any(each.ancestors[-1] is cell.ancestors[-1] for cell in blank)
        }
        self.assertEqual(marked, {"dash", "empty", "empty value", "line break"})
        self.assertEqual(len(blank), 4, "a table inside a cell is that cell's content")

    def test_each_cell_carries_the_width_its_column_leaves_below_however_wide_the_table(self):
        # Fourteen columns: the name, then each that may leave, the far end first.
        headings = "".join(f'{{% table_heading "C{n}" drop={15 - n} %}}' for n in range(2, 15))
        cells = "".join(f'<td data-col="{n}">{n}</td>' for n in range(3, 15))
        nested = "<table><tbody><tr><td>a</td><td>b</td><td>c</td></tr></tbody></table>"
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" %}' + headings + "</tr></thead><tbody>"
            '<tr class="row-group"><th colspan="14">Group</th></tr>'
            f'<tr><th>one</th><td data-col="2">2</td>{cells}</tr>'
            f'<tr><th>two</th><td data-col="2">{nested}</td>{cells}</tr>'
            '<tr><td colspan="14">Nothing else</td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        html = _render(source)
        leaves = {heading.text.strip(): heading.attrs.get("data-drop-at") for heading in elements(html, "th")}
        self.assertIsNone(leaves["Name"])
        # The far end leaves first, so at the widest.
        widths = [int(leaves[f"C{n}"]) for n in range(14, 1, -1)]
        self.assertEqual(widths, sorted(widths, reverse=True))
        found = elements(html, "td")
        for cell in (each for each in found if "data-col" in each.attrs):
            with self.subTest(column=cell.attrs["data-col"]):
                self.assertEqual(cell.attrs.get("data-drop-at"), leaves[f"C{cell.attrs['data-col']}"])
        others = [each for each in found if "data-col" not in each.attrs]
        self.assertEqual(len(others), 4, "the three cells of the table in a cell, and the one spanning the row")
        self.assertEqual([each for each in others if "data-drop-at" in each.attrs], [])
        (table,) = [each for each in all_elements(html) if each.tag == "table" and "data-table" in each.classes]
        self.assertEqual(table.attrs["data-narrow-at"], str(min(widths)))

    def test_a_column_with_nothing_in_any_row_is_marked_in_every_row(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Pick" select_all=True %}'
            '{% table_heading "Name" %}{% table_heading "Empty" %}{% table_heading "Some" %}</tr></thead><tbody>'
            '<tr class="row-group"><th colspan="4">Group</th></tr>'
            '<tr><td data-col="pick"></td><td data-col="name">—</td><td data-col="empty">—</td><td data-col="some">Said</td></tr>'
            '<tr><td data-col="pick"></td><td data-col="name">—</td><td data-col="empty"> </td><td data-col="some">–</td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        marked = [each for each in all_elements(_render(source)) if "data-blank-column" in each.attrs]
        # Never the name's, though it says nothing: a column of tick-boxes stands before it.
        self.assertEqual(
            [each.attrs.get("data-col") or each.text.strip() for each in marked], ["Empty", "empty", "empty"]
        )

    def test_what_a_kept_line_says_is_one_part_the_stylesheet_can_hold(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "State" "status" %}{% table_heading "Note" %}</tr></thead><tbody>'
            '<tr><td>a</td><td data-col="said">Ask me first <span class="muted">for\nthis</span></td><td>b</td></tr>'
            '<tr><td>c</td><td data-col="blank">—</td><td>d</td></tr>'
            '<tr><td colspan="3">Nothing else</td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        kept = with_class(_render(source), "kept-line")
        self.assertEqual([each.ancestors[-1].attrs.get("data-col") for each in kept], ["said"])
        self.assertEqual(kept[0].words, "Ask me first for this")

    def test_only_a_long_cell_of_prose_or_a_status_holds_out_for_a_floor(self):
        long = "A sentence long enough to stand <em>several</em> lines tall in a narrow column"
        lines = "Two lines, each of them short<br>and the second one as short"
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "State" "status" %}{% table_heading "Note" %}</tr></thead><tbody>'
            f'<tr><td>{long}</td><td data-col="status">{long}</td><td data-col="long">{long}</td></tr>'
            f'<tr><td>b</td><td>Up</td><td data-col="lines">{lines}</td></tr>'
            '<tr><td>c</td><td>Up</td><td data-col="blank"><span class="empty-value">——————————————————</span></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        marked = [each.attrs.get("data-col") for each in elements(_render(source), "td") if "data-long" in each.attrs]
        self.assertEqual(marked, ["status", "long"])

    def test_a_run_shown_whole_holds_its_column_open_and_a_cut_one_does_not(self):
        run = "a-worker-image-with-a-deliberately-long-name"

        def narrow_at(cell):
            source = (
                '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
                '{% table_heading "Image" "identifier" %}</tr></thead>'
                f"<tbody><tr><td>a</td><td>{cell}</td></tr></tbody></table>{{% endtable_region %}}"
            )
            (table,) = [each for each in all_elements(_render(source)) if each.tag == "table"]
            return int(table.attrs["data-narrow-at"])

        whole = narrow_at(f'<code class="cell-whole"><span class="cut">{run}</span></code>')
        self.assertGreater(whole, narrow_at(f"<code>{run}</code>"))
        self.assertGreater(whole, narrow_at(f'<span class="cell-data">{run}</span>'))

    def test_a_rank_on_the_column_that_names_the_row_is_refused(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '<th data-drop="1">Name</th>{% table_heading "Value" %}</tr></thead>'
            "<tbody><tr><td>a</td><td>b</td></tr></tbody></table>{% endtable_region %}"
        )
        with self.assertRaises(ValueError):
            _render(source)

    def test_a_heading_refuses_what_cannot_be(self):
        for source in (
            '{% table_heading "Name" "color" %}',
            '{% table_heading "State" css="key-col" drop=1 %}',
            '{% table_heading "Name" sort="name" local_sort=True %}',
        ):
            with self.subTest(source), self.assertRaises(ValueError):
                _render(source)

    def test_a_control_column_may_drop(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "Actions" css="actions-col" hidden=True drop=1 %}</tr></thead>'
            "<tbody><tr><td>a</td><td><button>Go</button></td></tr></tbody></table>{% endtable_region %}"
        )
        _name, heading = elements(_render(source), "th")
        self.assertEqual(heading.attrs["data-kind"], "control")
        self.assertIn(int(heading.attrs["data-drop-at"]), DROP_STEPS)

    def test_a_heading_note_stands_beside_its_label(self):
        for source in (
            '{% table_heading "Agents" note="off" note_css="policy-off" %}',
            '{% table_heading "Agents" local_sort=True note="off" note_css="policy-off" %}',
        ):
            with self.subTest(source):
                html = _render(source)
                (note,) = [each for each in elements(html, "span") if "policy-off" in each.classes]
                self.assertEqual(note.text.strip(), "off")
                self.assertIn("Agents", elements(html, "th")[0].text)

    def test_an_unknown_argument_is_refused_when_the_template_compiles(self):
        with self.assertRaises(TemplateSyntaxError):
            _render('{% table_heading "Name" color="red" %}')

    def test_a_page_sorted_heading_draws_its_button(self):
        (heading,) = elements(_render('{% table_heading "Name" local_sort=True %}'), "th")
        self.assertEqual(heading.attrs["aria-sort"], "none")
        self.assertIn("data-sort-ready", heading.attrs)
        (button,) = select(_render('{% table_heading "Name" local_sort=True %}'), "data-sort-local")
        self.assertEqual(button.tag, "button")
        self.assertIn("table-sort-link", button.classes)
        self.assertEqual(button.attrs["aria-label"], "Sort by Name")

    def test_a_server_sorted_heading_links_to_its_next_order(self):
        request = RequestFactory().get("/things/", {"sort": "name", "page": "3", "q": "a"})
        html = _render('{% table_heading "Name" sort="name" %}', {"table": {"selected_sort": "name"}}, request)
        (heading,) = elements(html, "th")
        self.assertEqual(heading.attrs["aria-sort"], "ascending")
        (link,) = elements(html, "a")
        self.assertIn("sort=-name", link.attrs["href"])
        self.assertIn("q=a", link.attrs["href"])
        self.assertNotIn("page=", link.attrs["href"])

    def test_a_hidden_label_is_still_read(self):
        html = _render('{% table_heading "Actions" css="actions-col" hidden=True %}')
        (heading,) = elements(html, "th")
        self.assertEqual(heading.attrs["data-kind"], "control")
        self.assertEqual(heading.text.strip(), "Actions")
        self.assertTrue(any("visually-hidden" in each.classes for each in elements(html, "span")))

    def test_the_select_all_heading_holds_its_tick_box(self):
        html = _render('{% table_heading "Select all rows" select_all=True %}')
        self.assertIn("select-column", elements(html, "th")[0].classes)
        (box,) = select(html, "data-select-all")
        self.assertEqual(box.attrs["aria-label"], "Select all rows")

    def test_rows_sort_on_the_page_only_when_there_are_two(self):
        source = '{% table_heading "Name" local_sort=rows|sortable %}'
        (one,) = elements(_render(source, {"rows": ["a"]}), "th")
        self.assertNotIn("aria-sort", one.attrs)
        (two,) = elements(_render(source, {"rows": ["a", "b"]}), "th")
        self.assertEqual(two.attrs["aria-sort"], "none")

    def test_grouped_rows_sort_by_their_count(self):
        source = '{% table_heading "Name" local_sort=count|sortable %}'
        (one,) = elements(_render(source, {"count": 1}), "th")
        self.assertNotIn("aria-sort", one.attrs)
        (two,) = elements(_render(source, {"count": 2}), "th")
        self.assertEqual(two.attrs["aria-sort"], "none")

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

    def test_a_row_toggle_starts_closed(self):
        html = _render('<td class="row-gutter">a{% row_toggle %}</td>')
        (toggle,) = select(html, "data-row-toggle")
        self.assertEqual(toggle.attrs["aria-expanded"], "false")
        self.assertIn("row-expand", toggle.classes)


class TableColumnTests(SimpleTestCase):
    def test_a_columns_kind_follows_from_its_classes(self):
        self.assertEqual(TableColumn("Cost", css="num-col").kind, "numeric")
        self.assertEqual(TableColumn("When", css="date-col").kind, "status")
        self.assertEqual(TableColumn("Cost", css="num-col", kind="prose").kind, "prose")

    def test_a_columns_kind_otherwise_follows_from_the_field_it_sorts_by(self):
        from hq.domains.expenses.models import Expense

        columns = typed(
            (
                TableColumn("Vendor", "vendor"),
                TableColumn("Date", "date"),
                TableColumn("Cost", "total_cost"),
                TableColumn("Category", "category"),
                TableColumn("Note"),
                TableColumn("Who", "no_such_field"),
                TableColumn("Kept", "total_cost", kind="prose"),
            ),
            Expense,
        )
        self.assertEqual(
            [column.kind for column in columns], ["prose", "status", "numeric", "status", "prose", "prose", "prose"]
        )

    def test_a_kept_column_has_no_drop_rank(self):
        with self.assertRaises(ValueError):
            TableColumn("Cost", css="key-col", drop=1)
        with self.assertRaises(ValueError):
            TableColumn("Note", drop=-1)

    def test_the_column_that_names_each_row_never_drops(self):
        with self.assertRaises(ValueError):
            named_first((TableColumn("Name", drop=1), TableColumn("Note")))
        columns = (TableColumn("Name"), TableColumn("Note", drop=1))
        self.assertEqual(named_first(columns), columns)

    def _ranks(self, *columns):
        return {column.label: column.drop for column in ranked(columns)}

    def test_a_list_ranks_its_columns_optional_first_then_from_the_far_end(self):
        ranks = self._ranks(
            TableColumn("Name"),
            TableColumn("Who", css="optional-col"),
            TableColumn("Action"),
            TableColumn("What", css="key-col"),
            TableColumn("Message"),
        )
        self.assertEqual(ranks, {"Name": 0, "Who": 1, "Message": 2, "Action": 3, "What": 0})

    def test_the_closing_figure_of_a_table_with_no_key_stays(self):
        figures = (TableColumn("Name"), TableColumn("Note"), TableColumn("Cost", css="num-col"))
        self.assertEqual(self._ranks(*figures), {"Name": 0, "Note": 1, "Cost": 0})
        keyed = (TableColumn("Name"), TableColumn("State", css="key-col"), TableColumn("Cost", css="num-col"))
        self.assertEqual(self._ranks(*keyed), {"Name": 0, "State": 0, "Cost": 1})

    def _list_ranks(self, **counts):
        view = type("Listed", (TableListMixin,), {"table_columns": _LIST_COLUMNS, **counts})()
        view.request = RequestFactory().get("/")
        return {column.label: column.drop for column in view.table_context()["columns"]}

    def test_a_list_ranks_its_columns_whatever_it_counts(self):
        ranks = {"Name": 0, "Note": 0, "When": 1}
        self.assertEqual(self._list_ranks(), ranks)
        self.assertEqual(self._list_ranks(table_drop_mid=1, table_drop_narrow=2), ranks)

    def test_a_declared_rank_stands(self):
        ranks = self._ranks(TableColumn("Name"), TableColumn("Note", drop=2), TableColumn("When", drop=1))
        self.assertEqual(ranks, {"Name": 0, "Note": 2, "When": 1})


class ListTableTests(_AuthedTestCase):
    """The shared list draws its table through the tags, rows and all."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from hq.platform.core import example_estate

        with example_estate.throwaway_media():
            example_estate.seed()

    def test_a_list_sits_in_its_region_with_declared_headings(self):
        page = self.client.get(reverse("expenses:list"))
        (region,) = select(page, "role", "region")
        self.assertTrue(region.attrs["aria-label"])
        headings = elements(page, "th")
        self.assertTrue(headings)
        self.assertTrue(all(heading.attrs.get("data-kind") for heading in headings))
        # The frame's row block still fills the table from inside the region.
        rows = [each for each in within(page, "role", "region") if each.tag == "tr"]
        self.assertGreater(len(rows), 1)

    def test_a_sort_mark_is_drawn_by_the_stylesheet_never_by_a_face(self):
        """A glyph is drawn by whichever face has it, at that face's size, so
        the mark holds no text: its arrow is the stylesheet's, read from the
        heading's `aria-sort`."""

        marks = with_class(self.client.get(reverse("expenses:list")), "sort-indicator")
        self.assertTrue(marks)
        self.assertEqual([mark.text for mark in marks if mark.text], [])

    def test_a_list_drops_its_ranked_columns(self):
        page = self.client.get(reverse("expenses:list"))
        leaves = {heading.text.strip(): heading.attrs.get("data-drop-at") for heading in elements(page, "th")}
        dropped = {label for label, width in leaves.items() if width}
        self.assertTrue(dropped)
        # The column that names the row and the key column stay.
        self.assertFalse(dropped & {"Vendor", "Cost"})

    def test_every_list_leads_each_row_with_its_name(self):
        """A row's name looks the same on every list: its first cell leads with
        it, set as a name, so no list reads as a column of plain links."""

        lists = [
            route.name
            for route in routed()
            if route.name
            and "<" not in route.path
            and isinstance(route.view, type)
            and issubclass(route.view, TableListMixin)
        ]
        self.assertGreater(len(lists), 5, "The walk found almost no lists")
        read = 0
        for name in lists:
            found = all_elements(self.client.get(reverse(name)).content.decode())
            names = [
                cell
                for cell in found
                if cell.tag == "td"
                and "row-gutter" in cell.classes
                and any(each.tag == "tbody" for each in cell.ancestors)
            ]
            read += bool(names)
            for cell in names:
                with self.subTest(name, row=cell.words):
                    lead = next(
                        (each for each in found if each.tag == "strong" and any(up is cell for up in each.ancestors)),
                        None,
                    )
                    self.assertIsNotNone(lead)
                    self.assertTrue(lead.words and cell.words.startswith(lead.words))
        self.assertGreater(read, 5, "The example estate fills almost no list")


class FitTests(SimpleTestCase):
    """Where a table's columns leave is derived from what each holds."""

    # The audit log's columns, as its list declares them, with the longest
    # line of each in characters: a type and name, a time, a user, an action
    # and a message.
    LONGEST = {"What": 40, "When": 12, "Who": 8, "Action": 7, "Message": 90}

    def _audit(self):
        """The list's columns, ranked as it ranks them, each with its floor."""

        columns = ranked(typed(AuditLogListView.table_columns, AuditLogListView.model))
        floors = [
            column_floor(
                column.kind, self.LONGEST[column.label], label=len(column.label), sorts=True, names_row=not index
            )
            for index, column in enumerate(columns)
        ]
        return columns, floors

    def test_each_column_leaves_below_the_floors_of_what_still_shows(self):
        columns, floors = self._audit()
        table = fit(tuple(FitColumn(floor, column.drop) for floor, column in zip(floors, columns, strict=True)))
        self.assertEqual({columns[index].label for index in table.leaves}, {"Who", "Message", "Action"})
        showing = set(range(len(columns)))
        for index in sorted(table.leaves, key=lambda index: columns[index].drop):
            width = table.leaves[index]
            needed = sum(floors[each] for each in showing)
            with self.subTest(columns[index].label):
                # It stays only where every column showing has its floor, and
                # leaves at the first declared width past that.
                self.assertIn(width, DROP_STEPS)
                self.assertGreaterEqual(width, needed)
                self.assertTrue(all(step < needed for step in DROP_STEPS if step < width))
            showing.discard(index)
        self.assertEqual(table.narrow, min(table.leaves.values()))

    def test_a_status_keeps_its_whole_line_and_prose_holds_out_for_its_floor(self):
        columns, floors = self._audit()
        kinds = {column.label: column.kind for column in columns}
        self.assertEqual((kinds["When"], kinds["Action"], kinds["Message"]), ("status", "status", "prose"))
        floor = dict(zip(kinds, floors, strict=True))
        self.assertGreater(floor["When"], floor["Who"])
        self.assertGreater(column_floor("status", 30), column_floor("prose", 30))
        self.assertEqual(floor["Message"], column_floor("prose", 200))
        # A status too long for one line wraps at the prose floor.
        self.assertEqual(column_floor("status", 90), column_floor("prose", 90))

    def test_columns_of_one_rank_leave_together_and_the_share_counts_what_shows(self):
        columns = (FitColumn(200), FitColumn(150, 1), FitColumn(150, 1), FitColumn(40, shares=False), FitColumn(100, 2))
        table = fit(columns)
        self.assertEqual(table.leaves[1], table.leaves[2])
        self.assertEqual(table.columns, 4)
        self.assertEqual(dict(table.columns_below), {table.leaves[1]: 2, table.leaves[4]: 1})

    def test_a_table_with_nothing_to_let_go_is_narrow_below_its_floors(self):
        table = fit((FitColumn(300), FitColumn(130)))
        self.assertEqual((table.leaves, table.narrow), ({}, drop_step(430)))
