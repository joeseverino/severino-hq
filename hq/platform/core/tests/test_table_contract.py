"""A data table is declared (``tables.DataTable``), so its shape is in the HTML on first paint."""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.template import Context, RequestContext, Template
from django.template.autoreload import get_template_directories
from django.test import RequestFactory, SimpleTestCase, tag
from django.urls import reverse

from hq.platform.application import asks
from hq.platform.application.asks import Ask, Standing
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.markup_testing import all_elements, elements, links, select, with_class, within
from hq.platform.application.moments import when
from hq.platform.application.money import money
from hq.platform.application.pages import PageAction
from hq.platform.application.routes import routed
from hq.platform.application.tables import Below, Cell, DataTable, Group, Row, TableColumn, typed
from hq.platform.application.ui import MISSING, measured
from hq.platform.core.part_examples import table
from hq.platform.core.stylesheets import stylesheet as whole_stylesheet
from hq.platform.core.table_layout import TRACKS
from hq.platform.core.testing import SignedInTestCase, leaving

TEMPLATES = Path(__file__).resolve().parents[4] / "templates"


def _template_roots() -> list[Path]:
    """The host's templates and each installed extension's, so the composed
    suite reads the extensions as well."""

    return sorted(get_template_directories())


@tag("source", "tree")
class TableContractTests(SimpleTestCase):
    maxDiff = None

    def test_no_template_writes_what_the_region_derives(self):
        """A figure's alignment follows from its heading's kind and the sort
        control from the rows the table holds, so no template spells either
        on a cell or a heading."""

        derived = re.compile(r"\|\s*sortable\b|\bnum-col\b")
        found = [
            f"{path.relative_to(root)}:{text.count(chr(10), 0, match.start()) + 1}: {match.group()}"
            for root in _template_roots()
            for path in sorted(root.rglob("*.html"))
            for text in [path.read_text(encoding="utf-8")]
            for match in derived.finditer(text)
        ]
        self.assertEqual(found, [], "The declared table derives these; drop them from the template.")

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
        self.assertIn("row-opened", marked)
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


def _drawn(table: DataTable, source: str = "{% data_table table %}") -> str:
    return _render(source, {"table": table})


def _things(*columns, rows=(), **options) -> DataTable:
    return DataTable(
        "Things", tuple(TableColumn(*each) if isinstance(each, tuple) else each for each in columns), rows, **options
    )


def _row(*cells, **options) -> Row:
    return Row(tuple(each if isinstance(each, Cell) else Cell(each) for each in cells), **options)


def _cells(html: str) -> list:
    """Each body and foot cell, in order."""

    return [
        each
        for each in all_elements(html)
        if each.tag == "td" and not any("row-detail" in up.classes for up in each.ancestors)
    ]


class TableTagTests(SimpleTestCase):
    def test_a_box_that_scrolls_is_a_stop_named_for_its_table(self):
        """A table fits its box by letting columns go, so only a pane and a
        chart's table scroll; each is drawn as a stop for the keyboard, named
        so the stop says what it holds, and no other box is one."""

        for region, stop in (({}, False), ({"pane": True}, True), ({"chart": True}, True)):
            with self.subTest(region):
                html = _drawn(table("Machines", ("Name",), ("a",), region=region))
                (box,) = with_class(html, "table-scroll")
                said = (box.attrs.get("role"), box.attrs.get("aria-label"), box.attrs.get("tabindex"))
                self.assertEqual(said, ("group", "Machines", "0") if stop else (None, None, None))
                self.assertEqual("is-pane" in box.classes, "pane" in region)

    def test_a_section_and_the_table_in_it_are_one_landmark(self):
        """A table in a section of its own name, the section drawn by its tag
        or by hand as a card, adds no second landmark of that name, whether it
        fits or scrolls."""

        frames = {
            "section": ('{% section "Records" %}', "{% endsection %}"),
            "card": ('{% card heading="records" %}<h2 id="records">Records</h2>', "{% endcard %}"),
        }
        for frame, (opening, closing) in frames.items():
            for region in ({}, {"pane": True}, {"chart": True}):
                with self.subTest(frame=frame, region=region):
                    records = table("Records", ("Name",), ("a",), region=region)
                    landmarks = _landmarks(_drawn(records, opening + "{% data_table table %}" + closing))
                    self.assertEqual(landmarks.count(("region", "records")), 1, landmarks)

    def test_a_table_with_no_rows_is_drawn_as_the_empty_state(self):
        runs = table("Latest runs", (TableColumn("Name", kind="identifier"),))
        html = _drawn(runs)
        self.assertEqual((elements(html, "table"), with_class(html, "table-scroll")), ([], []))
        (empty,) = select(html, "data-empty-state")
        self.assertIn("Latest runs", empty.words)
        html = _drawn(table("Latest runs", ("Name",), ("a",)))
        self.assertEqual((len(elements(html, "table")), select(html, "data-empty-state")), (1, []))

    def test_an_empty_table_says_what_is_missing_or_that_its_filter_emptied_it(self):
        """A table told what its emptiness means says so with what would fill
        it; one its own filter emptied says nothing matches, with the way
        back."""

        fill = {"label": "Add a run", "url": "/runs/new/"}
        for region, says, offers in (
            ({"empty": "No runs yet.", "remedy": fill}, "No runs yet.", ("Add a run", "/runs/new/")),
            (
                {"empty": "No runs yet.", "action_url": "/runs/new/", "action_label": "Add a run"},
                "No runs yet.",
                ("Add a run", "/runs/new/"),
            ),
            (
                {"empty": "No runs yet.", "remedy": fill, "filtered": True, "clear": "/runs/"},
                "Nothing matches.",
                ("Clear filters", "/runs/"),
            ),
        ):
            with self.subTest(region):
                html = _drawn(table("Runs", ("Name",), region=region))
                (empty,) = select(html, "data-empty-state")
                self.assertTrue(empty.words.startswith(says), empty.words)
                ((words, href),) = links(html)
                self.assertEqual((words.startswith(offers[0]), href), (True, offers[1]))

    def test_what_would_fill_a_table_is_asked_only_when_it_is_empty(self):
        """A remedy may read the database (`page.remedy`), so a table with
        rows, which never shows one, never asks for it."""

        asked = []

        def remedy():
            asked.append(True)
            return {"label": "Add a run", "url": "/runs/new/"}

        _drawn(table("Runs", ("Name",), ("a",), region={"remedy": remedy}))
        self.assertEqual(asked, [])
        ((words, href),) = links(_drawn(table("Runs", ("Name",), region={"remedy": remedy})))
        self.assertEqual((words.startswith("Add a run"), href), (True, "/runs/new/"))
        self.assertEqual(asked, [True])

    def test_a_cell_that_says_nothing_is_marked_blank(self):
        cells = {"dash": "\u2014", "space": " ", "nothing": "", "words": "Served", "link": Cell("Go", url="/go/")}
        rows = tuple(_row(name, cell, data={"probe": name}) for name, cell in cells.items())
        found = all_elements(_drawn(_things(("Name",), ("Value",), rows=rows)))
        blank = {
            cell.ancestors[-1].attrs["data-probe"] for cell in found if cell.tag == "td" and "data-blank" in cell.attrs
        }
        self.assertEqual(blank, {"dash", "space", "nothing"})

    def test_a_column_with_nothing_in_any_row_is_marked_in_every_row(self):
        rows = (_row("\u2014", "\u2014", "Said", key=1), _row("\u2014", " ", "–", key=2))
        drawn = _drawn(_things(("Name",), ("Empty",), ("Some",), groups=(Group("Group", rows),), selectable=True))
        marked = [each for each in all_elements(drawn) if "data-blank-column" in each.attrs]
        # Never the name's, though it says nothing: a column of tick-boxes stands before it.
        self.assertEqual([(each.tag, each.words) for each in marked], [("th", "Empty"), ("td", "\u2014"), ("td", "")])

    def test_a_rows_toggle_is_a_child_of_its_gutter_cell(self):
        # The stylesheet shows a cut row's toggle as the gutter cell's own child.
        html = _drawn(_things(TableColumn("When", kind="status"), ("Note",), rows=(_row("Nov 2", "b"),)))
        (toggle,) = [each for each in elements(html, "button") if "data-row-toggle" in each.attrs]
        self.assertEqual(toggle.ancestors[-1].tag, "td")

    def test_only_a_cut_cell_with_a_long_line_is_clamped(self):
        """A value with a long line is held to a few lines where its row
        opens, and its row holds the toggle that shows it whole; the name the
        row is read by and a status are never held, nor is a short line or a
        blank cell."""

        long = "A sentence long enough to stand several lines tall in a narrow column"
        rows = (_row(long, long, long), _row("b", "Up", "Two lines, each of them short"), _row("c", "Up", "\u2014"))
        html = _drawn(_things(("Name",), TableColumn("State", kind="status"), ("Note",), rows=rows))
        found = all_elements(html)
        cells = _cells(html)
        clamped = [
            at
            for at, cell in enumerate(cells)
            if any("cell-clamp" in each.classes for each in found if each is cell or cell in each.ancestors)
        ]
        self.assertEqual(clamped, [2])
        # An open row releases its clamp in place, so the value is never
        # copied to the open line.
        self.assertFalse([each for each in found if each.tag == "dl"])
        self.assertTrue([each for each in found if cells[0] in each.ancestors and "data-row-toggle" in each.attrs])
        self.assertFalse(any("data-below" in each.attrs for each in found if each.tag == "tr"))

    def test_a_row_that_holds_something_back_has_its_line_beneath(self):
        """After a row is the line its toggle opens: the blocks it keeps
        beneath itself, then each labeled block under its label."""

        rich = _row(
            Cell("A", below=(Below("", (Cell("tall"),)),)),
            Cell("Said", below=(Below("Also", (Cell("x"),)),)),
            "Up",
            data={"probe": "rich"},
        )
        rows = (rich, _row("B", "–", "Up", data={"probe": "plain"}))
        found = all_elements(
            _drawn(_things(("Name",), TableColumn("Note", drop=1), TableColumn("State", kind="status"), rows=rows))
        )
        rows = [each for each in found if each.tag == "tr" and any(up.tag == "tbody" for up in each.ancestors)]
        self.assertEqual(
            [row.attrs.get("data-probe", row.attrs.get("class")) for row in rows], ["rich", "row-detail", "plain"]
        )
        beneath = [each for each in found if rows[1] in each.ancestors]
        (cell,) = [each for each in beneath if each.tag == "td"]
        self.assertEqual(cell.attrs["colspan"], "3")
        self.assertEqual([each.words for each in beneath if "row-below" in each.classes], ["tall", "x"])
        entries = [each for each in beneath if each.tag == "div" and each.ancestors[-1].tag == "dl"]
        self.assertEqual([entry.words for entry in entries], ["Also x"])

    def test_a_rows_ask_says_its_word_and_sentence_on_the_line_beneath(self):
        """An ask in a row shows its state's word alone there, so the line the
        row opens to says the word and the sentence under the button's label;
        an ask that has said nothing adds nothing to it."""

        failed = Ask(
            "Refresh",
            "/example/ask/",
            Standing(asks.FAILED, "The example could not be read."),
            title="Read the example again.",
        )
        found = all_elements(_drawn(_things(("Name",), rows=(_row("A", asks=(failed,)),))))
        # The title holds the failure; what the button is for stays apart, for
        # ask.js to say again once the work has nothing to say.
        (button,) = [each for each in found if "data-ask-button" in each.attrs]
        self.assertEqual(button.attrs["data-ask-reason"], "Read the example again.")
        (detail,) = [each for each in found if "row-detail" in each.classes]
        beneath = [each for each in found if detail in each.ancestors]
        self.assertEqual([each.words for each in beneath if each.tag == "dt"], ["Refresh"])
        (said,) = [each for each in beneath if "data-ask-why" in each.attrs]
        self.assertEqual(said.attrs["data-ask-why"], "/example/ask/")
        self.assertIn("Failed", said.words)
        self.assertIn("could not be read", said.words)
        idle = _things(("Name",), rows=(_row("A", asks=(Ask("Refresh", "/example/ask/"),)),))
        self.assertEqual(with_class(_drawn(idle), "row-detail"), [])

    def test_the_line_a_script_adds_beneath_a_row_is_the_one_drawn_there(self):
        """ask.js clones the frame _ask.html draws for a row whose ask says
        something once the row is drawn, so its entry nests as a drawn one's
        does, the label beside the value."""

        failed = Ask("Refresh", "/example/ask/", Standing(asks.FAILED, "The example could not be read."))

        def nesting(markup):
            found = all_elements(markup)
            ((why, detail),) = [
                (each, up)
                for each in found
                if "data-ask-why" in each.attrs
                for up in each.ancestors
                if "data-row-detail" in up.attrs
            ]
            (value,) = [up for up in why.ancestors if up.tag == "dd"]
            terms = [each.tag for each in found if each.tag == "dt" and value.ancestors[-1] in each.ancestors]
            return [
                each.tag for each in why.ancestors[why.ancestors.index(detail) : why.ancestors.index(value) + 1]
            ], terms

        drawn = _drawn(_things(("Name",), rows=(_row("A", asks=(failed,)),)))
        frame = _render('{% include "partials/_ask.html#frames" %}')
        (below,) = re.findall(r"<template data-ask-below>(.*?)</template>", frame, re.DOTALL)
        self.assertEqual(nesting(below), nesting(drawn))

    def test_a_row_with_nothing_held_back_has_no_line_beneath(self):
        rows = (_row("a", "–"), _row("Nothing else", across=True))
        self.assertEqual(
            with_class(_drawn(_things(("Name",), TableColumn("Note", drop=1), rows=rows)), "row-detail"), []
        )

    def test_a_status_heading_with_a_rank_leaves_by_it_and_one_without_stays(self):
        """A ranked status is a secondary check, which leaves with the
        table's ranked columns whatever its cells say; an unranked one is the
        row's state, which leaves last."""

        columns = (
            TableColumn("Name"),
            TableColumn("Note", drop=1),
            TableColumn("Check", kind="status", drop=2),
            TableColumn("State", kind="status"),
        )
        cells = _cells(_drawn(_things(*columns, rows=(_row("a", "b", "Passed", "Up"),))))
        note, check, state = (cell.attrs.get("data-drop") for cell in cells[1:4])
        self.assertTrue(check)
        self.assertLessEqual(int(check), int(note))
        self.assertGreater(int(state), int(check))

    def test_a_column_refuses_what_cannot_be(self):
        for options in ({"kind": "color"}, {"css": "key-col", "drop": 1}):
            with self.subTest(options), self.assertRaises(ValueError):
                TableColumn("Name", **options)

    def test_a_page_sorted_heading_draws_its_button(self):
        html = _drawn(_things(("Name",), rows=(_row("a"), _row("b"))))
        (heading,) = elements(html, "th")
        self.assertEqual(heading.attrs["aria-sort"], "none")
        self.assertIn("data-sort-ready", heading.attrs)
        (button,) = select(html, "data-sort-local")
        self.assertEqual(button.tag, "button")
        self.assertIn("table-sort-link", button.classes)
        self.assertEqual(button.attrs["aria-label"], "Sort by Name")

    def test_a_server_sorted_heading_links_to_its_next_order(self):
        request = RequestFactory().get("/things/", {"q": "x", "page": "3", "sort": "name"})
        table = _things(("Name", "name"), rows=(_row("a"), _row("b")), sorted_by="name")
        html = _render("{% data_table table %}", {"table": table}, request)
        (heading,) = elements(html, "th")
        self.assertEqual(heading.attrs["aria-sort"], "ascending")
        (link,) = elements(html, "a")
        self.assertEqual(parse_qs(urlsplit(link.attrs["href"]).query), {"q": ["x"], "sort": ["-name"]})
        self.assertEqual(select(html, "data-sort-local"), [])

    def test_pagination_links_keep_the_filters_and_set_the_page(self):
        request = RequestFactory().get("/things/", {"q": "x", "sort": "name", "page": "2"})
        context = {"pages": {"previous": 1, "next": 3, "number": 2, "of": 3}}
        html = _render('{% include "partials/_pagination.html" %}', context, request)
        queries = [parse_qs(urlsplit(link.attrs["href"]).query) for link in elements(html, "a")]
        self.assertEqual(queries, [{"q": ["x"], "sort": ["name"], "page": [str(n)]} for n in (1, 3)])

    def test_a_hidden_label_is_still_read(self):
        html = _drawn(
            _things(
                ("Name",), rows=(_row("a", actions=(PageAction._built(label="Remove", url="/example/1/remove/"),)),)
            )
        )
        heading = elements(html, "th")[-1]
        self.assertEqual(heading.attrs["data-kind"], "control")
        self.assertEqual(heading.text.strip(), "Actions")
        self.assertTrue(any("visually-hidden" in each.classes for each in elements(html, "span")))

    def test_rows_sort_on_the_page_only_when_there_are_two(self):
        """A heading that sorts on the page keeps its control where its table
        holds two rows to put in order, counted across its groups; a row that
        spans the table holds its place and is not one."""

        for groups, sorts in (
            ((("A", ("a",)),), False),
            ((("A", ("a",)), ("B", ())), False),
            ((("A", ("a", "b")),), True),
            ((("A", ("a",)), ("B", ("b",))), True),
        ):
            with self.subTest(groups):
                declared = tuple(Group(label, tuple(_row(each, "1") for each in rows)) for label, rows in groups)
                across = Group("", (_row("Nothing else", across=True),))
                html = _drawn(_things(("Name",), TableColumn("Cost", kind="numeric"), groups=(*declared, across)))
                headings = [each for each in elements(html, "th") if each.attrs.get("scope") != "rowgroup"]
                self.assertEqual([each.words for each in headings], ["Name", "Cost"])
                self.assertEqual([each.attrs.get("aria-sort") for each in headings], ["none" if sorts else None] * 2)
                self.assertEqual(["data-sort-ready" in each.attrs for each in headings], [sorts] * 2)
                self.assertEqual(len(select(html, "data-sort-local")), 2 if sorts else 0)
                self.assertEqual(bool(with_class(html, "sort-indicator")), sorts)

    def test_a_heading_sorts_on_the_page_unless_it_holds_controls(self):
        """A heading that says nothing of its order sorts the rows on the page
        where there are two to put in order; a column of controls, one named
        for assistive technology alone and the tick-box column never do, nor
        a heading told to keep the table's own order."""

        columns = {
            "name": TableColumn("Name", kind="identifier"),
            "cost": TableColumn("Cost", kind="numeric"),
            "kept": TableColumn("Step", sorts=False),
            "control": TableColumn("Link", kind="control"),
            "actions": TableColumn("Actions", css="actions-col"),
            "hidden": TableColumn("Note", hidden=True),
        }
        for keys, sorting in (((1, 2), {"name", "cost"}), ((1,), set())):
            with self.subTest(rows=len(keys)):
                rows = tuple(_row(*["a"] * len(columns), key=key) for key in keys)
                html = _drawn(_things(*columns.values(), rows=rows, selectable=True))
                ready = {
                    name
                    for name, each in zip(("select", *columns), elements(html, "th"), strict=True)
                    if "data-sort-ready" in each.attrs
                }
                self.assertEqual(ready, sorting)
                self.assertEqual(len(select(html, "data-sort-local")), len(sorting))

    def test_a_charts_table_keeps_the_charts_order(self):
        """A chart's table is the chart read whole, in its order, so its
        headings sort nothing."""

        weeks = table(
            "Example sessions", (TableColumn("Week", kind="status"),), ("Week 1",), ("Week 2",), region={"chart": True}
        )
        html = _drawn(weeks)
        self.assertEqual(select(html, "data-sort-local"), [])
        self.assertNotIn("data-sort-ready", elements(html, "th")[0].attrs)

    def test_a_cell_sorts_by_the_value_it_declares(self):
        """A table that sorts on the page writes on each cell whose value is
        not its words what it orders by: a figure's number however its words
        read, a moment's instant. Words and a cell that says nothing write
        none, nor does a table that keeps its own order."""

        stamp = date(2026, 1, 2)
        rows = (
            _row(money(Decimal("-1234.5")), measured(4.25, "mi"), Cell(moment=stamp), "Beta"),
            _row(money(Decimal(9)), when(stamp), Cell(), MISSING),
        )
        columns = ("Cost", "Distance", "When", "Name")
        sorted_by = [
            each.attrs.get("data-sort") for each in _cells(_drawn(_things(*[(c,) for c in columns], rows=rows)))
        ]
        epoch = str(datetime(2026, 1, 2, tzinfo=UTC).timestamp())
        self.assertEqual(sorted_by, ["-1234.50", "4.25", epoch, None, "9.00", epoch, None, None])
        kept = _drawn(_things(*[(c,) for c in columns], rows=rows, keeps_order=True))
        self.assertEqual([each.attrs.get("data-sort") for each in _cells(kept)], [None] * 8)

    def test_a_figures_cells_align_as_its_heading_says(self):
        """A numeric column's cells are marked with its kind, body and totals
        alike, so the stylesheet aligns them under their heading; a cell that
        spans the table is no column's."""

        rows = (_row("a", "1", "b"), _row("Both", across=True))
        figures = _things(
            ("Name",),
            TableColumn("Cost", kind="numeric"),
            ("Note",),
            groups=(Group("Group", rows),),
            footer=_row("Total", "1", ""),
        )
        kinds = [cell.attrs.get("data-kind") for cell in _cells(_drawn(figures))]
        self.assertEqual(kinds, ["name", "numeric", "prose", None, "name", "numeric", "prose"])


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
            leaving(TableColumn("Name", drop=1), TableColumn("Note"))
        self.assertEqual(leaving(TableColumn("Name"), TableColumn("Note", drop=1)), {"Name": 0, "Note": 1})

    def test_a_table_draws_no_more_columns_than_its_grid_has_tracks(self):
        columns = tuple(TableColumn(f"Column {at}") for at in range(TRACKS))
        acted = Row(tuple(Cell(column.label) for column in columns), actions=(PageAction("Open", "/"),))
        DataTable("Things", columns)
        for refused in (
            lambda: DataTable("Things", (*columns, TableColumn("Over"))),
            lambda: DataTable("Things", columns, (acted,)),
        ):
            with self.subTest(refused), self.assertRaises(ValueError):
                refused()

    def test_a_table_lets_its_columns_go_at_the_narrowest_widths_optional_first_then_from_the_far_end(self):
        levels = leaving(
            TableColumn("Name"),
            TableColumn("Who", css="optional-col"),
            TableColumn("Action"),
            TableColumn("When", css="date-col"),
            TableColumn("State", kind="status"),
            TableColumn("What", css="key-col"),
            TableColumn("Message"),
        )
        self.assertEqual(levels, {"Name": 0, "Who": 1, "Message": 2, "State": 3, "Action": 4, "When": 5, "What": 0})
        self.assertEqual(
            leaving(TableColumn("Name"), TableColumn("Note"), TableColumn("Who")), {"Name": 0, "Note": 4, "Who": 3}
        )

    def test_the_closing_figure_of_a_table_with_no_key_stays(self):
        figures = (TableColumn("Name"), TableColumn("Note"), TableColumn("Cost", kind="numeric"))
        self.assertEqual(leaving(*figures), {"Name": 0, "Note": 4, "Cost": 0})
        keyed = (TableColumn("Name"), TableColumn("State", css="key-col"), TableColumn("Cost", kind="numeric"))
        self.assertEqual(leaving(*keyed), {"Name": 0, "State": 0, "Cost": 4})

    def test_a_column_of_moments_is_a_date(self):
        rows = (Row((Cell("a"), Cell(moment=date(2026, 3, 1)))), Row((Cell("b"), Cell())))
        _, day = DataTable("Things", (TableColumn("Name"), TableColumn("Day")), rows).columns
        self.assertEqual(day.css, "date-col")

    def test_a_declared_rank_stands(self):
        levels = leaving(TableColumn("Name"), TableColumn("Note", drop=2), TableColumn("When", drop=1))
        self.assertEqual(levels, {"Name": 0, "Note": 2, "When": 1})


class ListTableTests(SignedInTestCase):
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
            and issubclass(route.view, ListPageMixin)
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
