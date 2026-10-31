"""A data table is declared through the table tags, so its shape is in the HTML on first paint."""

import re
from pathlib import Path

from django.template import Context, RequestContext, Template, TemplateSyntaxError
from django.template.autoreload import get_template_directories
from django.test import RequestFactory, SimpleTestCase, tag
from django.urls import reverse

from hq.platform.application import asks
from hq.platform.application.asks import Ask, Standing
from hq.platform.application.plugin_testing import hand_drawn_tables, table_problems
from hq.platform.application.routes import routed
from hq.platform.application.tables import TableColumn, TableListMixin, ranked, typed
from hq.platform.application.tests.markup import all_elements, elements, select, with_class, within
from hq.platform.core.stylesheets import stylesheet as whole_stylesheet

from .test_web import _AuthedTestCase

TEMPLATES = Path(__file__).resolve().parents[4] / "templates"

# A list's columns: the name, a column kept at every width, and one that may leave.
_LIST_COLUMNS = (TableColumn("Name"), TableColumn("Note", css="key-col"), TableColumn("When"))


def _template_roots() -> list[Path]:
    """The host's templates and each installed extension's, so the composed
    suite reads the extensions as well."""

    return sorted(get_template_directories())


# What tables.js marks as it fits a table to its region, before the frame
# that draws the table is laid out.
FITTED = {"dropped", "narrow"}


@tag("source", "tree")
class TableContractTests(SimpleTestCase):
    maxDiff = None

    def test_every_table_is_declared_through_the_tags(self):
        problems = [problem for root in _template_roots() for problem in hand_drawn_tables(root)]
        self.assertEqual(problems, [], "Draw these tables with {% table_region %} and {% table_heading %}.")

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
        html = _render(
            '{% table_region "Machines" %}<table class="data-table"><tbody><tr><td>a</td></tr></tbody></table>'
            "{% endtable_region %}"
        )
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

    def test_a_table_with_no_rows_is_drawn_as_the_empty_state(self):
        """Whatever left the rows out (a loop over nothing, a branch not
        taken), the region draws its empty state instead of headings over
        nothing; a row in the body, or a region holding no table, is drawn."""

        head = '<thead><tr>{% table_heading "Name" "identifier" %}</tr></thead>'
        body = "<tbody>{% for row in rows %}<tr><td>{{ row }}</td></tr>{% endfor %}</tbody>"
        source = (
            '{% table_region "Latest runs" %}<table class="data-table">' + head + body + "</table>{% endtable_region %}"
        )
        for rows in ([], None):
            with self.subTest(rows=rows):
                html = _render(source, {"rows": rows})
                self.assertEqual(elements(html, "table"), [])
                self.assertEqual(select(html, "role", "region"), [])
                (empty,) = select(html, "data-empty-state")
                self.assertIn("Latest runs", empty.words)
        html = _render(source, {"rows": ["a"]})
        self.assertEqual(len(elements(html, "table")), 1)
        self.assertEqual(select(html, "data-empty-state"), [])

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

    def test_a_column_s_kind_is_said_once_on_its_heading(self):
        """The stylesheet reads a column's kind down from its heading, so no
        body cell repeats it: the region writes on a cell only what the
        heading cannot say."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "State" "status" %}{% table_heading "Act" css="actions-col" %}</tr></thead><tbody>'
            "<tr><td>a</td><td>Up</td><td><button>Go</button></td></tr></tbody></table>{% endtable_region %}"
        )
        html = _render(source)
        self.assertEqual(
            [heading.attrs["data-kind"] for heading in elements(html, "th")], ["prose", "status", "control"]
        )
        self.assertEqual([cell for cell in elements(html, "td") if "data-kind" in cell.attrs], [])

    def test_a_heading_carries_its_rank(self):
        source = (
            '{% table_heading "Name" %}{% table_heading "A" drop=1 %}{% table_heading "B" drop=3 %}'
            '{% table_heading "D" css="key-col" %}'
        )
        ranks = {heading.text.strip(): heading.attrs.get("data-drop") for heading in elements(_render(source), "th")}
        self.assertEqual(ranks, {"Name": None, "A": "1", "B": "3", "D": None})

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
            '<tr><td data-col="pick"></td><td data-col="name">\u2014</td><td data-col="empty">\u2014</td><td data-col="some">Said</td></tr>'
            '<tr><td data-col="pick"></td><td data-col="name">\u2014</td><td data-col="empty"> </td><td data-col="some">–</td></tr>'
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
            '<tr><td>c</td><td data-col="blank">\u2014</td><td>d</td></tr>'
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
            f'<tr><td>c</td><td>Up</td><td data-col="blank"><span class="empty-value">{"\u2014" * 18}</span></td></tr>'
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

    def test_a_row_that_holds_something_back_has_its_line_beneath(self):
        """After a row is the line its toggle opens: the blocks it keeps
        beneath itself, then under its column's name a column that may leave
        (its blocks taken out of the copy), a narrow part of a column that
        stays, and a labeled block. A control under no heading is named for
        a screen reader alone, and a column with nothing to show has no part."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" gutter=True %}'
            '{% table_heading "Note" drop=1 %}{% table_heading "State" "status" css="key-col" %}'
            '{% table_heading "Blank" drop=2 %}{% table_heading "Act" hidden=True css="actions-col" drop=3 %}'
            "</tr></thead><tbody>"
            '<tr data-probe="rich"><td class="row-gutter">A <span class="narrow-more">from <b>here</b></span>'
            '<div class="row-below">tall</div>{% row_toggle %}</td>'
            '<td class="cell-data">Said <ul class="row-below" data-label="Also">x</ul></td>'
            '<td>Up <span class="narrow-more">since</span></td><td>–</td><td><button>Go</button></td></tr>'
            '<tr data-probe="plain"><td class="row-gutter">B{% row_toggle %}</td><td>–</td><td>Up</td><td>–</td>'
            "<td>–</td></tr>"
            "</tbody></table>{% endtable_region %}"
        )
        found = all_elements(_render(source))
        rows = [each for each in found if each.tag == "tr" and any(up.tag == "tbody" for up in each.ancestors)]
        self.assertEqual(
            [row.attrs.get("data-probe", row.attrs.get("class")) for row in rows], ["rich", "row-detail", "plain"]
        )
        detail = rows[1]
        beneath = [each for each in found if detail in each.ancestors]
        (cell,) = [each for each in beneath if each.tag == "td"]
        self.assertEqual(cell.attrs["colspan"], "5")
        self.assertEqual([each.words for each in beneath if "row-below" in each.classes], ["tall", "x"])
        terms = [each for each in beneath if each.tag == "dt"]
        self.assertEqual([each.words for each in terms], ["Name", "Note", "State", "Act", "Also"])
        self.assertEqual(
            [each.attrs.get("data-column") or each.attrs.keys() & {"data-narrow", "data-label"} for each in terms],
            [{"data-narrow"}, "1", {"data-narrow"}, "4", {"data-label"}],
        )
        self.assertEqual([each.classes for each in terms], [frozenset()] * 5)
        values = [each for each in beneath if each.tag == "dd"]
        self.assertEqual([each.words for each in values], ["from here", "Said", "since", "Go", "x"])
        self.assertIn("cell-data", values[1].classes)

    def test_a_rows_ask_says_its_word_and_sentence_on_the_line_beneath(self):
        """An ask in a row shows its state's word alone there, so the line the
        row opens to says the word and the sentence under the button's label;
        an ask that has said nothing adds nothing to it."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" gutter=True %}'
            '{% table_heading "Act" hidden=True %}</tr></thead><tbody>'
            '<tr><td class="row-gutter">A{% row_toggle %}</td><td>{% include "partials/_ask.html" with ask=ask %}</td>'
            "</tr></tbody></table>{% endtable_region %}"
        )
        failed = Ask(
            "Refresh",
            "/example/ask/",
            Standing(asks.FAILED, "The example could not be read."),
            title="Read the example again.",
        )
        found = all_elements(_render(source, {"ask": failed}))
        # The title holds the failure; what the button is for stays apart, for
        # ask.js to say again once the work has nothing to say.
        (button,) = [each for each in found if "data-ask-button" in each.attrs]
        self.assertEqual(button.attrs["data-ask-reason"], "Read the example again.")
        (detail,) = [each for each in found if "row-detail" in each.classes]
        beneath = [each for each in found if detail in each.ancestors]
        self.assertEqual([each.words for each in beneath if each.tag == "dt"], ["Refresh"])
        (said,) = [each for each in beneath if "data-ask-why" in each.attrs]
        self.assertIn("Failed", said.words)
        self.assertIn("could not be read", said.words)
        self.assertEqual(said.attrs["data-ask-why"], "/example/ask/")
        idle = Ask("Refresh", "/example/ask/")
        self.assertEqual(with_class(_render(source, {"ask": idle}), "row-detail"), [])

    def test_a_control_under_no_heading_is_named_for_a_screen_reader_alone(self):
        source = (
            '{% table_region "Queue" %}<table class="data-table"><tbody>'
            '<tr><td class="row-gutter">{% row_toggle %}</td><td>What</td><td><a class="narrow-more" href="/x">Fix</a></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        (term,) = [element for element in all_elements(_render(source)) if element.tag == "dt"]
        self.assertEqual(term.words, "Actions")
        self.assertIn("visually-hidden", term.classes)

    def test_a_row_with_nothing_held_back_has_no_line_beneath(self):
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "Note" drop=1 %}</tr></thead><tbody><tr><td>a</td><td>–</td></tr>'
            '<tr><td colspan="2">Nothing else</td></tr></tbody></table>{% endtable_region %}'
        )
        self.assertEqual(with_class(_render(source), "row-detail"), [])

    def test_a_cell_left_without_its_end_tag_holds_nothing_back(self):
        """Its content has no end to copy up to, so the open line shows only
        what the row's closed cells hold."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "Note" drop=1 %}{% table_heading "When" drop=2 %}</tr></thead><tbody>'
            "<tr><td>a</td><td>Open<td>Shut</td></tr></tbody></table>{% endtable_region %}"
        )
        values = [each.words for each in all_elements(_render(source)) if each.tag == "dd"]
        self.assertEqual(values, ["Shut"])

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
            ranked((TableColumn("Name", drop=1), TableColumn("Note")))
        columns = (TableColumn("Name"), TableColumn("Note", drop=1))
        self.assertEqual(ranked(columns), columns)

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

    def test_a_list_ranks_its_columns(self):
        view = type("Listed", (TableListMixin,), {"table_columns": _LIST_COLUMNS, "model": None})()
        view.request = RequestFactory().get("/")
        ranks = {column.label: column.drop for column in view.table_context()["columns"]}
        self.assertEqual(ranks, {"Name": 0, "Note": 0, "When": 1})

    def test_a_declared_rank_stands(self):
        ranks = self._ranks(TableColumn("Name"), TableColumn("Note", drop=2), TableColumn("When", drop=1))
        self.assertEqual(ranks, {"Name": 0, "Note": 2, "When": 1})


class ListTableTests(_AuthedTestCase):
    """The shared list draws its table through the tags, rows and all."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from hq.platform.core import example_estate
        from hq.platform.core.example_estate.testing import throwaway_media

        with throwaway_media():
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
