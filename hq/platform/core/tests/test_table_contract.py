"""A data table is declared through the table tags, so its shape is in the HTML on first paint."""

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.core.paginator import Paginator
from django.template import Context, RequestContext, Template, TemplateSyntaxError
from django.template.autoreload import get_template_directories
from django.test import RequestFactory, SimpleTestCase, tag
from django.urls import reverse

from hq.platform.application import asks
from hq.platform.application.asks import Ask, Standing
from hq.platform.application.plugin_testing import hand_drawn_tables, table_problems
from hq.platform.application.routes import routed
from hq.platform.application.tables import TableColumn, TableListMixin, ranked, typed
from hq.platform.application.tests.markup import all_elements, elements, links, select, with_class, within
from hq.platform.application.tests.reads import reads
from hq.platform.core.stylesheets import stylesheet as whole_stylesheet

from .signed_in_case import _AuthedTestCase

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
@reads("**/templates/**", "static/css/**", "static/js/**")
class TableContractTests(SimpleTestCase):
    maxDiff = None

    def test_every_table_is_declared_through_the_tags(self):
        problems = [problem for root in _template_roots() for problem in hand_drawn_tables(root)]
        self.assertEqual(problems, [], "Draw these tables with {% table_region %} and {% table_heading %}.")

    def test_no_template_writes_what_the_region_derives(self):
        """A figure's alignment follows from its heading's kind and the sort
        control from the rows the table holds, so no template spells either
        on a cell or a heading (a gutter's toggle written by hand is refused
        as the region renders)."""

        derived = re.compile(r"\|\s*sortable\b|\bnum-col\b")
        found = [
            f"{path.relative_to(root)}:{text.count(chr(10), 0, match.start()) + 1}: {match.group()}"
            for root in _template_roots()
            for path in sorted(root.rglob("*.html"))
            for text in [path.read_text(encoding="utf-8")]
            for match in derived.finditer(text)
        ]
        self.assertEqual(found, [], "The table region derives these; drop them from the template.")

    def test_a_hand_drawn_table_is_refused(self):
        bare = '<table class="data-table"><thead><tr><th>Name</th></tr></thead></table>'
        self.assertIn("a data table outside {% table_region %}", table_problems(bare))
        wrapped = f'<div class="table-scroll">{bare}</div>'
        self.assertIn("a scroll region written by hand", table_problems(wrapped))
        in_region = '{% table_region "Things" %}' + bare + "{% endtable_region %}"
        self.assertEqual(table_problems(in_region), ["a heading not drawn by {% table_heading %}"])

    def test_a_heading_outside_the_shared_table_is_refused(self):
        """The heading's sort control and the region's fit are drawn by the
        shared table's stylesheet, so a plain table there draws them as
        page buttons."""

        plain = (
            '{% table_region "Things" %}<table><thead><tr>{% table_heading "Name" %}</tr></thead>'
            "</table>{% endtable_region %}"
        )
        self.assertEqual(
            table_problems(plain),
            [
                'a table in {% table_region %} without class="data-table"',
                'a {% table_heading %} outside a table with class="data-table"',
            ],
        )
        loose = '{% table_heading "Name" %}'
        self.assertEqual(table_problems(loose), ['a {% table_heading %} outside a table with class="data-table"'])

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


# The landmark roles, and the element each is native to: a section or a form
# is one only once it is named (HTML-AAM).
_LANDMARKS = {"banner", "complementary", "contentinfo", "form", "main", "navigation", "region", "search"}
_NATIVE = {"aside": "complementary", "form": "form", "main": "main", "nav": "navigation", "section": "region"}


def _landmarks(html: str) -> list[tuple[str, str]]:
    """Each landmark drawn, as its role and its name, the pair axe's
    landmark-unique holds to be one of a kind on a page."""

    drawn = all_elements(html)
    by_id = {element.attrs["id"]: element for element in drawn if "id" in element.attrs}
    found = []
    for element in drawn:
        role = element.attrs.get("role") or _NATIVE.get(element.tag)
        if role not in _LANDMARKS:
            continue
        named = element.attrs.get("aria-labelledby", "").split()
        name = element.attrs.get("aria-label") or " ".join(by_id[each].words for each in named if each in by_id)
        if name.strip() or role not in {"form", "region"}:
            found.append((role, " ".join(name.split()).lower()))
    return found


_ROWS = '<table class="data-table"><tbody><tr><td>a</td></tr></tbody></table>'


class TableTagTests(SimpleTestCase):
    def test_every_region_is_drawn_as_a_stop_named_for_its_table(self):
        """Without script any region may scroll, so the server draws each one
        as a stop for the keyboard, named so the stop says what it holds;
        tables.js takes it off a region whose table fits."""

        for options in ("", " pane=True", " chart=True", ' room="shared"', " compare=True"):
            with self.subTest(options=options):
                html = _render('{% table_region "Machines"' + options + " %}" + _ROWS + "{% endtable_region %}")
                (region,) = with_class(html, "table-scroll")
                self.assertEqual(
                    (region.attrs.get("role"), region.attrs.get("aria-label"), region.attrs.get("tabindex")),
                    ("group", "Machines", "0"),
                )
        (pane,) = with_class(_render('{% table_region "Links" pane=True %}{% endtable_region %}'), "table-scroll")
        self.assertIn("is-pane", pane.classes)

    def test_a_section_and_the_table_in_it_are_one_landmark(self):
        """A region in a section of its own name, the section drawn by its tag
        or by hand as a card, adds no second landmark of that name, whether it
        fits or scrolls."""

        frames = {
            "section": ('{% section "Records" %}', "{% endsection %}"),
            "card": ('{% card heading="records" %}<h2 id="records">Records</h2>', "{% endcard %}"),
        }
        for frame, (opening, closing) in frames.items():
            for options in ("", " pane=True", " chart=True"):
                with self.subTest(frame=frame, options=options):
                    region = '{% table_region "Records"' + options + " %}" + _ROWS + "{% endtable_region %}"
                    landmarks = _landmarks(_render(opening + region + closing))
                    self.assertEqual(landmarks.count(("region", "records")), 1, landmarks)

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
                self.assertEqual(with_class(html, "table-scroll"), [])
                (empty,) = select(html, "data-empty-state")
                self.assertIn("Latest runs", empty.words)
        html = _render(source, {"rows": ["a"]})
        self.assertEqual(len(elements(html, "table")), 1)
        self.assertEqual(select(html, "data-empty-state"), [])

    def test_an_empty_region_says_what_is_missing_or_that_its_filter_emptied_it(self):
        """A region told what its empty tables mean says so with what would
        fill them; one its own filter emptied says nothing matches, with the
        way back, in place of the table and only there."""

        table = (
            '<table class="data-table"><thead><tr>{% table_heading "Name" "identifier" %}</tr></thead>'
            "<tbody>{% for row in rows %}<tr><td>{{ row }}</td></tr>{% endfor %}</tbody></table>"
        )
        fill = {"label": "Add a run", "url": "/runs/new/"}
        for options, says, offers in (
            ('empty="No runs yet." remedy=fill', "No runs yet.", ("Add a run", "/runs/new/")),
            (
                'empty="No runs yet." action_url="/runs/new/" action_label="Add a run"',
                "No runs yet.",
                ("Add a run", "/runs/new/"),
            ),
            (
                'empty="No runs yet." remedy=fill filtered=True clear="/runs/"',
                "Nothing matches.",
                ("Clear filters", "/runs/"),
            ),
        ):
            source = "{% table_region " + f'"Runs" {options}' + " %}" + table + "{% endtable_region %}"
            with self.subTest(options):
                html = _render(source, {"rows": [], "fill": fill})
                self.assertEqual(elements(html, "table"), [])
                (empty,) = select(html, "data-empty-state")
                self.assertTrue(empty.words.startswith(says), empty.words)
                ((words, href),) = links(html)
                self.assertEqual((words.startswith(offers[0]), href), (True, offers[1]))
                html = _render(source, {"rows": ["a"], "fill": fill})
                self.assertEqual(len(elements(html, "table")), 1)
                self.assertEqual(select(html, "data-empty-state"), [])

    def test_what_would_fill_a_region_is_asked_only_when_it_is_empty(self):
        """A remedy may read the database (`page.remedy`), so a region with
        rows, which never shows one, never asks for it."""

        asked = []

        class Page:
            @property
            def remedy(self):
                asked.append(True)
                return {"label": "Add a run", "url": "/runs/new/"}

        source = (
            '{% table_region "Runs" empty="No runs yet." remedy=page.remedy %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" "identifier" %}</tr></thead>'
            "<tbody>{% for row in rows %}<tr><td>{{ row }}</td></tr>{% endfor %}</tbody></table>{% endtable_region %}"
        )
        _render(source, {"rows": ["a"], "page": Page()})
        self.assertEqual(asked, [])
        ((words, href),) = links(_render(source, {"rows": [], "page": Page()}))
        self.assertEqual((words.startswith("Add a run"), href), (True, "/runs/new/"))
        self.assertEqual(asked, [True])

    def test_a_region_names_the_room_its_identifiers_get(self):
        for room in ("shared", "snug"):
            with self.subTest(room):
                source = '{% table_region "Services" room="' + room + '" %}{% endtable_region %}'
                (region,) = with_class(_render(source), "table-scroll")
                self.assertIn(f"is-{room}", region.classes)
        (plain,) = with_class(_render('{% table_region "Services" %}{% endtable_region %}'), "table-scroll")
        self.assertFalse({"is-shared", "is-snug"} & set(plain.classes))

    def test_a_compare_region_says_so(self):
        (region,) = with_class(
            _render('{% table_region "Changes" compare=True %}{% endtable_region %}'), "table-scroll"
        )
        self.assertIn("is-compare", region.classes)
        (plain,) = with_class(_render('{% table_region "Changes" %}{% endtable_region %}'), "table-scroll")
        self.assertNotIn("is-compare", plain.classes)

    def test_a_room_no_share_is_named_for_is_refused(self):
        with self.assertRaises(ValueError):
            _render('{% table_region "Services" room="wide" %}{% endtable_region %}')

    def test_a_chart_region_says_so(self):
        html = _render('{% table_region "Runs" chart=True %}{% endtable_region %}')
        self.assertIn("data-chart", with_class(html, "table-scroll")[0].attrs)

    def test_a_heading_declares_its_kind(self):
        (heading,) = elements(_render('{% table_heading "Cost" "numeric" %}'), "th")
        self.assertEqual(heading.attrs["data-kind"], "numeric")
        (named,) = elements(_render('{% table_heading "Name" "identifier" %}'), "th")
        self.assertEqual(named.attrs["data-kind"], "identifier")

    def test_a_column_s_kind_is_said_once_on_its_heading(self):
        """The stylesheet reads a column's kind from its heading, so a body
        cell repeats it only where the heading cannot say it down the column:
        a figure's alignment and a control's size."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "State" "status" %}{% table_heading "Act" css="actions-col" %}</tr></thead><tbody>'
            "<tr><td>a</td><td>Up</td><td><button>Go</button></td></tr></tbody></table>{% endtable_region %}"
        )
        html = _render(source)
        self.assertEqual(
            [heading.attrs["data-kind"] for heading in elements(html, "th")], ["prose", "status", "control"]
        )
        self.assertEqual([cell.attrs.get("data-kind") for cell in elements(html, "td")], [None, None, "control"])

    def test_every_cell_under_a_control_heading_is_marked_a_control(self):
        """tables.css sizes a select or a field to what it holds only in a
        cell marked a control, so every cell under such a heading is, however
        the heading names its kind."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "Group" "control" %}{% table_heading "Act" css="actions-col" %}</tr></thead><tbody>'
            "<tr><td>a</td><td><select><option>No group</option></select></td><td><button>Go</button></td></tr>"
            "<tr><td>b</td><td><select><option>Cash</option></select></td><td></td></tr>"
            "</tbody></table>{% endtable_region %}"
        )
        cells = [cell.attrs.get("data-kind") for cell in elements(_render(source), "td")]
        self.assertEqual(cells, [None, "control", "control", None, "control", "control"])

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

    def test_a_kept_line_leaves_the_rows_toggle_a_child_of_its_cell(self):
        # The stylesheet shows a cut row's toggle as the gutter cell's own child.
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "When" "status" gutter=True %}{% table_heading "Note" %}</tr></thead><tbody>'
            "<tr><td>Nov 2</td><td>b</td></tr>"
            "</tbody></table>{% endtable_region %}"
        )
        html = _render(source)

        (toggle,) = [each for each in elements(html, "button") if "data-row-toggle" in each.attrs]
        self.assertEqual(toggle.ancestors[-1].tag, "td")
        self.assertEqual([each.words for each in with_class(html, "kept-line")], ["Nov 2"])

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

    def test_the_words_beside_a_rows_controls_keep_their_line(self):
        long = "A sentence long enough to wrap beside a row's buttons"
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "State" "status" %}{% table_heading "Note" %}<th></th></tr></thead><tbody>'
            f'<tr><td data-col="short">a</td><td>Up</td><td data-col="long">{long}</td>'
            '<td class="row-actions"><button>Read</button></td></tr>'
            f'<tr><td>b</td><td>Up</td><td>{long}</td><td class="select-column"><input type="checkbox"></td></tr>'
            "</tbody></table>{% endtable_region %}"
        )
        marked = {
            each.attrs.get("data-col"): each.attrs["data-words"]
            for each in elements(_render(source), "td")
            if "data-words" in each.attrs
        }
        self.assertEqual(marked, {"short": "line", "long": "floor"})

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

    def test_a_status_said_by_one_pill_alone_is_marked_to_take_its_columns_width(self):
        """So the pills down a status column share both edges, whatever their words."""

        cells = {
            "one": '{% status "Changed" tone="attention" %}',
            "other": '{% status "New" tone="good" %}',
            "linked": '{% status "Open" href="/example/" %}',
            "beside words": '{% status "Up" tone="good" %} since Nov 2',
            "two": '{% status "Up" %}{% status "Pinned" %}',
            "words": "Up",
            "blank": "",
        }
        rows = "".join(f'<tr><td>a</td><td data-col="{name}">{cell}</td></tr>' for name, cell in cells.items())
        prose = '<tr><td>b</td><td>x</td><td data-col="prose">{% status "Up" %}</td></tr>'
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            f'{{% table_heading "State" "status" %}}</tr></thead><tbody>{rows}</tbody></table>{{% endtable_region %}}'
            '{% table_region "Others" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            f'{{% table_heading "Kind" %}}{{% table_heading "Note" %}}</tr></thead><tbody>{prose}</tbody></table>'
            "{% endtable_region %}"
        )
        cells = elements(_render("{% load status_tags %}" + source), "td")
        marked = [each.attrs.get("data-col") for each in cells if "data-pill-only" in each.attrs]
        self.assertEqual(marked, ["one", "other", "linked"])

    def test_a_row_that_holds_something_back_has_its_line_beneath(self):
        """After a row is the line its toggle opens: the blocks it keeps
        beneath itself, then under its column's name a column that may leave
        (its blocks taken out of the copy), a narrow part of a column that
        stays, and a labeled block. A column with nothing to show has no part,
        and the row's controls have none, whatever their rank, since they stand
        beneath its words in a narrow box."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" gutter=True %}'
            '{% table_heading "Note" drop=1 %}{% table_heading "State" "status" css="key-col" %}'
            '{% table_heading "Blank" drop=2 %}{% table_heading "Act" hidden=True css="actions-col" drop=3 %}'
            "</tr></thead><tbody>"
            '<tr data-probe="rich"><td>A <span class="narrow-more">from <b>here</b></span>'
            '<div class="row-below">tall</div></td>'
            '<td class="cell-data">Said <ul class="row-below" data-label="Also">x</ul></td>'
            '<td>Up <span class="narrow-more">since</span></td><td>–</td><td><button>Go</button></td></tr>'
            '<tr data-probe="plain"><td>B</td><td>–</td><td>Up</td><td>–</td>'
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
        self.assertEqual([each.words for each in terms], ["Name", "Note", "State", "Also"])
        self.assertEqual(
            [each.attrs.get("data-column") or each.attrs.keys() & {"data-narrow", "data-label"} for each in terms],
            [{"data-narrow"}, "1", {"data-narrow"}, {"data-label"}],
        )
        self.assertEqual([each.classes for each in terms], [frozenset()] * 4)
        values = [each for each in beneath if each.tag == "dd"]
        self.assertEqual([each.words for each in values], ["from here", "Said", "since", "x"])
        self.assertEqual(values[1].attrs.get("data-cell"), "data")

    def test_each_part_of_a_cell_is_marked_with_its_role(self):
        """The region marks what a part of a cell is, so the stylesheet keys
        its cut, hold and wrap on that mark and never on a cell's content: the
        bold a cell leads with (or its leading link's) names the row, a
        declared class says the rest, and a copy on the open line keeps every
        role but the name, which names nothing there."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}'
            '{% table_heading "Said" drop=1 %}{% table_heading "State" "status" %}</tr></thead><tbody>'
            '<tr><td><strong data-probe="lead">a</strong><br><span class="muted" data-probe="quiet">b</span></td>'
            '<td class="muted cell-data" data-probe="free"><strong data-probe="said">c</strong></td>'
            '<td><strong data-probe="kept">Up</strong></td></tr>'
            '<tr><td><a href="/x"><strong data-probe="linked">d</strong></a></td>'
            '<td><br><strong data-probe="second">e</strong> <span class="cell-prose" data-probe="means">f</span></td>'
            "<td>Up</td></tr>"
            "</tbody></table>{% endtable_region %}"
        )
        found = all_elements(_render(source))
        on_row = {
            each.attrs["data-probe"]: each.attrs.get("data-cell")
            for each in found
            if "data-probe" in each.attrs and not any("row-detail" in up.classes for up in each.ancestors)
        }
        self.assertEqual(
            on_row,
            {
                "lead": "name",
                "quiet": None,
                "free": "data",
                "said": "name",
                "kept": None,
                "linked": "name",
                "second": None,
                "means": "prose",
            },
        )
        copied = {
            each.attrs["data-probe"]: each.attrs.get("data-cell")
            for each in found
            if "data-probe" in each.attrs and any("row-detail" in up.classes for up in each.ancestors)
        }
        self.assertEqual(copied, {"said": None, "second": None, "means": "prose"})

    def test_a_rows_ask_says_its_word_and_sentence_on_the_line_beneath(self):
        """An ask in a row shows its state's word alone there, so the line the
        row opens to says the word and the sentence under the button's label;
        an ask that has said nothing adds nothing to it."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>{% table_heading "Name" gutter=True %}'
            '{% table_heading "Act" hidden=True %}</tr></thead><tbody>'
            '<tr><td>A</td><td>{% include "partials/_ask.html" with ask=ask %}</td>'
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
        request = RequestFactory().get("/things/", {"q": "x", "page": "3", "sort": "name"})
        html = _render('{% table_heading "Name" sort="name" %}', {"table": {"selected_sort": "name"}}, request)
        (heading,) = elements(html, "th")
        self.assertEqual(heading.attrs["aria-sort"], "ascending")
        (link,) = elements(html, "a")
        self.assertEqual(parse_qs(urlsplit(link.attrs["href"]).query), {"q": ["x"], "sort": ["-name"]})
        self.assertEqual(select(html, "data-sort-local"), [])

    def test_pagination_links_keep_the_filters_and_set_the_page(self):
        request = RequestFactory().get("/things/", {"q": "x", "sort": "name", "page": "2"})
        paginator = Paginator(range(30), 10)
        context = {"is_paginated": True, "page_obj": paginator.page(2), "paginator": paginator}
        html = _render('{% include "partials/_pagination.html" %}', context, request)
        queries = [parse_qs(urlsplit(link.attrs["href"]).query) for link in elements(html, "a")]
        self.assertEqual(queries, [{"q": ["x"], "sort": ["name"], "page": [str(n)]} for n in (1, 3)])

    def test_a_hidden_label_is_still_read(self):
        html = _render('{% table_heading "Actions" css="actions-col" hidden=True %}')
        (heading,) = elements(html, "th")
        self.assertEqual(heading.attrs["data-kind"], "control")
        self.assertEqual(heading.text.strip(), "Actions")
        self.assertTrue(any("visually-hidden" in each.classes for each in elements(html, "span")))

    def test_rows_sort_on_the_page_only_when_there_are_two(self):
        """A heading that sorts on the page keeps its control where its table
        holds two rows to put in order, counted across its groups; a row that
        spans the table holds its place and is not one."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" local_sort=True %}{% table_heading "Cost" "numeric" local_sort=True %}'
            "</tr></thead>{% for group in groups %}<tbody>{% row_group group.0 %}"
            "{% for row in group.1 %}<tr><td>{{ row }}</td><td>1</td></tr>{% endfor %}</tbody>{% endfor %}"
            '<tbody><tr><td colspan="2">Nothing else</td></tr></tbody></table>{% endtable_region %}'
        )
        for groups, sorts in (
            ((("A", ("a",)),), False),
            ((("A", ("a",)), ("B", ())), False),
            ((("A", ("a", "b")),), True),
            ((("A", ("a",)), ("B", ("b",))), True),
        ):
            with self.subTest(groups):
                html = _render(source, {"groups": groups})
                headings = [each for each in elements(html, "th") if each.attrs.get("scope") != "rowgroup"]
                self.assertEqual([each.words for each in headings], ["Name", "Cost"])
                self.assertEqual([each.attrs.get("aria-sort") for each in headings], ["none" if sorts else None] * 2)
                self.assertEqual(["data-sort-ready" in each.attrs for each in headings], [sorts] * 2)
                self.assertEqual(len(select(html, "data-sort-local")), 2 if sorts else 0)
                self.assertEqual(bool(with_class(html, "sort-indicator")), sorts)

    def test_a_tables_rows_are_counted_for_its_own_headings(self):
        """Two tables in one region: each heading's control follows from its
        own table's rows."""

        table = (
            '<table class="data-table"><thead><tr>{% table_heading "Name" local_sort=True %}</tr></thead>'
            "<tbody>{% for row in rows %}<tr><td>{{ row }}</td></tr>{% endfor %}</tbody></table>"
        )
        source = '{% table_region "Things" %}' + table.replace("rows", "one") + table.replace("rows", "two")
        html = _render(source + "{% endtable_region %}", {"one": ["a"], "two": ["a", "b"]})
        self.assertEqual([each.attrs.get("aria-sort") for each in elements(html, "th")], [None, "none"])

    def test_a_heading_sorts_on_the_page_unless_it_holds_controls(self):
        """A heading that says nothing of its order sorts the rows on the page
        where there are two to put in order; a column of controls, one named
        for assistive technology alone and the tick-box column never do, nor
        a heading told to keep the table's own order."""

        headings = {
            "name": '{% table_heading "Name" "identifier" %}',
            "cost": '{% table_heading "Cost" "numeric" %}',
            "kept": '{% table_heading "Step" local_sort=False %}',
            "control": '{% table_heading "Link" "control" %}',
            "actions": '{% table_heading "Actions" css="actions-col" %}',
            "hidden": '{% table_heading "Note" hidden=True %}',
            "select": '{% table_heading "Select all rows" select_all=True %}',
        }
        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            + "".join(headings.values())
            + "</tr></thead><tbody>{% for row in rows %}<tr>"
            + "<td>{{ row }}</td>" * len(headings)
            + "</tr>{% endfor %}</tbody></table>{% endtable_region %}"
        )
        for rows, sorting in ((["a", "b"], {"name", "cost"}), (["a"], set())):
            with self.subTest(rows=len(rows)):
                ready = {
                    name
                    for name, each in zip(headings, elements(_render(source, {"rows": rows}), "th"), strict=True)
                    if "data-sort-ready" in each.attrs
                }
                self.assertEqual(ready, sorting)
                self.assertEqual(len(select(_render(source, {"rows": rows}), "data-sort-local")), len(sorting))

    def test_a_charts_table_keeps_the_charts_order(self):
        """A chart's table is the chart read whole, in its order, so its
        headings sort nothing, asked or not."""

        for heading in ('{% table_heading "Week" "status" %}', '{% table_heading "Week" "status" local_sort=True %}'):
            source = (
                '{% table_region "Example sessions" chart=True %}<table class="data-table"><thead><tr>'
                + heading
                + "</tr></thead><tbody><tr><td>Week 1</td></tr><tr><td>Week 2</td></tr></tbody></table>"
                "{% endtable_region %}"
            )
            with self.subTest(heading):
                html = _render(source)
                self.assertEqual(select(html, "data-sort-local"), [])
                self.assertNotIn("data-sort-ready", elements(html, "th")[0].attrs)

    def test_a_figures_cells_align_as_its_heading_says(self):
        """A numeric column's cells are marked with its kind, body and totals
        alike, so the stylesheet aligns them under their heading; a cell that
        spans columns and a group's heading row are not a figure's."""

        source = (
            '{% table_region "Things" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" %}{% table_heading "Cost" "numeric" %}{% table_heading "Note" %}'
            '</tr></thead><tbody>{% row_group "Group" %}'
            '<tr><td data-probe="name">a</td><td data-probe="cost">1</td><td data-probe="note">b</td></tr>'
            '<tr><td colspan="2" data-probe="span">Both</td><td data-probe="after">c</td></tr>'
            '</tbody><tfoot><tr><td data-probe="label">Total</td><td data-probe="total">1</td><td></td></tr></tfoot>'
            "</table>{% endtable_region %}"
        )
        kinds = {
            each.attrs["data-probe"]: each.attrs.get("data-kind")
            for each in all_elements(_render(source))
            if "data-probe" in each.attrs
        }
        self.assertEqual(
            kinds,
            {
                "name": None,
                "cost": "numeric",
                "note": None,
                "span": None,
                "after": None,
                "label": None,
                "total": "numeric",
            },
        )


class TableColumnTests(SimpleTestCase):
    def test_a_columns_kind_follows_from_its_classes(self):
        self.assertEqual(TableColumn("Act", css="actions-col").kind, "control")
        self.assertEqual(TableColumn("When", css="date-col").kind, "status")
        self.assertEqual(TableColumn("When", css="date-col", kind="prose").kind, "prose")

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
        figures = (TableColumn("Name"), TableColumn("Note"), TableColumn("Cost", kind="numeric"))
        self.assertEqual(self._ranks(*figures), {"Name": 0, "Note": 1, "Cost": 0})
        keyed = (TableColumn("Name"), TableColumn("State", css="key-col"), TableColumn("Cost", kind="numeric"))
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
        self.assertEqual(len(with_class(page, "table-scroll")), 1)
        headings = elements(page, "th")
        self.assertTrue(headings)
        self.assertTrue(all(heading.attrs.get("data-kind") for heading in headings))
        # The frame's row block still fills the table from inside the region.
        rows = [each for each in within(page, "class", "table-scroll") if each.tag == "tr"]
        self.assertGreater(len(rows), 1)

    def test_a_lists_rows_keep_the_order_its_server_gives(self):
        """A list page holds one page of a list its server orders, so no
        heading sorts the rows on the page, where a sort would order a page."""

        page = self.client.get(reverse("expenses:list"))
        self.assertTrue(select(page, "aria-sort"))
        self.assertEqual(select(page, "data-sort-local"), [])

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
