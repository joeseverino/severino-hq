"""A data table is declared through the table tags, so its shape is in the HTML on first paint."""

import re
from pathlib import Path

from django.template import Context, RequestContext, Template, TemplateSyntaxError, engines
from django.template.base import TextNode
from django.template.defaulttags import IfNode
from django.test import RequestFactory, SimpleTestCase, tag
from django.urls import reverse

from hq.platform.application.routes import routed
from hq.platform.application.tables import TableColumn, TableListMixin, named_first, ranked, typed
from hq.platform.application.tests.markup import all_elements, elements, select, with_class, within
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


# What tables.js marks as it fits a table to its region: in a ResizeObserver,
# so in the frame the table is laid out, before that frame is painted.
FITTED = {"dropped", "narrow"}


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
        # A table's fit is marked in the frame it is laid out, before paint.
        marked -= FITTED
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

    def test_a_status_too_long_to_keep_wraps(self):
        long = "Ask me first for container stack and tailnet policy changes"
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "State" "status" %}</tr></thead><tbody>'
            f'<tr><td>a</td><td data-col="long">{long}</td></tr><tr><td>b</td><td data-col="short">Up</td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        kept = with_class(_render(source), "kept-line")
        self.assertEqual([each.ancestors[-1].attrs.get("data-col") for each in kept], ["short"])

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
