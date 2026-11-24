"""The tags a data table is drawn through: ``data_table``, which draws a
``DataTable`` whole, and the parts of a table it draws.

Which columns show is the browser's to say (``data_table``'s rules).
"""

from typing import Annotated, Any

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.describe import Describe
from hq.platform.application.tables import Cell, DataTable, Row, TableColumn, check_drop, column_kind
from hq.platform.core import table_layout
from hq.platform.core.part_examples import tags
from hq.platform.core.template_parts import Part, answers, slotted
lazy from hq.platform.core.drawn_tables import drawn

register = template.Library()


def _open_line(width: int, held: str, listed: list[tuple[str, str]]) -> str:
    """A row's open line: ``held`` first, then each listed entry's value
    under its label."""

    entries = [f"<div><dt>{escape(name)}</dt><dd>{value}</dd></div>" for name, value in listed]
    columns = f'<dl class="row-columns">{"".join(entries)}</dl>' if entries else ""
    return f'<tr class="row-detail" data-row-detail><td colspan="{width}">{held}{columns}</td></tr>'


# The open line a table whose rows hold cells back draws once, which
# tables.js clones for a row as it opens, an entry for each cell it holds back
# and a box for what the row's line already shows of one.
LINE_FRAME = f"<template data-open-line>{_open_line(1, '', [('', '<span data-row-shows></span>')])}</template>"


@register.simple_block_tag
def open_line(content):
    """An open line holding one unlabeled entry (``OPEN_LINE``'s rules)."""

    return mark_safe(_open_line(1, "", [("", content)]))  # noqa: S308 - the body is rendered template output, escaped there


# The template contract reads a tag's declaration from what it wraps.
OPEN_LINE = register.tags["open_line"].__wrapped__ = Part(  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    name="open_line",
    rules="""
    The line a row opens to that a script adds is `{% open_line %}value{% endopen_line %}`, the line
    the region draws, so it is the same part: a frame the script clones for a row that comes to hold
    something beneath it once drawn (ask.js), setting the label and width.
    """,
    about="The line a row opens to, holding one entry, as a frame a script clones for a row.",
    # The frame an ask clones for the row it stands in, as `_ask.html` draws it.
    example=(
        '<template data-example>{% open_line %}<span class="row-below" data-label="Example">An example note.</span>'
        "{% endopen_line %}</template>"
    ),
)


def _server_sort(context: Any, ascending: str, descending: str) -> dict[str, str]:
    """A list's own sort for this column: the order its link asks for and which way it stands."""

    current = context["table"]["selected_sort"]
    descending = descending or f"-{ascending}"
    next_sort = descending if current == ascending else ascending
    return {
        "next": next_sort,
        "direction": "ascending" if current == ascending else "descending" if current == descending else "none",
        "next_direction": "descending" if next_sort == descending else "ascending",
    }


def table_heading(
    context: Any,
    label: Annotated[str, Describe("The column's name.")],
    kind: Annotated[str, Describe("What the column holds, one of `COLUMN_KINDS`; left out, its classes say.")] = "",
    *,
    css: Annotated[str, Describe("The column's classes.")] = "",
    drop: Annotated[int, Describe("The rank it leaves by when the table is too wide, 1 first.")] = 0,
    sort: Annotated[str, Describe("The field a server-sorted list orders this column by.")] = "",
    descending: Annotated[str, Describe("Its reverse, when that is not `-field`.")] = "",
    local_sort: Annotated[
        bool | None, Describe("Sorts the rows on the page; left out, it follows from what the column holds.")
    ] = None,
    hidden: Annotated[bool, Describe("The name for assistive technology only.")] = False,
    title: Annotated[str, Describe("Its `title`.")] = "",
    select_all: Annotated[bool, Describe("The tick-box that selects every row.")] = False,
) -> dict[str, Any]:
    """One column heading (its Part's rules)."""

    if sort and local_sort:
        raise ValueError(f"Column {label!r} sorts on the server or on the page, not both.")
    kind = "control" if select_all else column_kind(kind, css)
    if local_sort is None:
        local_sort = not sort and kind != "control" and not hidden and bool(label)
    check_drop(drop, css, label)
    classes = [css] if css else []
    classes += ["select-column"] if select_all else []
    return {
        "label": label,
        "kind": kind,
        "css": " ".join(classes),
        "drop": drop,
        "server_sort": _server_sort(context, sort, descending) if sort else None,
        "local_sort": local_sort,
        "hidden": hidden,
        "title": title,
        "select_all": select_all,
    }


@register.filter
def data_attributes(data: Any) -> SafeString:
    """Each ``data-`` attribute a declaration names (``tables.held_data``), as
    the start tag it belongs to writes it."""

    return mark_safe("".join(f' data-{name}="{escape(value)}"' for name, value in data.items()))  # noqa: S308 - each value escaped, each name held


@register.filter
def chart_table(chart: Any) -> DataTable:
    """``{% data_table chart|chart_table %}``: what a chart (``ui.ChartDrawing``)
    draws, as a table read in its own order: a row per period or date, headed
    by it, a column per series, each value as its tooltip writes it."""

    columns = (
        TableColumn("Period" if chart.stacked else "Date", kind="status"),
        *(TableColumn(heading, kind="numeric") for heading in chart.table_headings),
    )
    rows = tuple(Row((Cell(row.label, header=True), *map(Cell, row.cells))) for row in chart.table_rows)
    return DataTable(chart.title, columns, rows, region={"chart": True})


@answers(register)
def cadence_table(matrix: Any, *, label: str = "", compact: bool = False) -> DataTable:
    """``{% cadence_table matrix label=... compact=... as table %}``: a
    cadence matrix as its table of marks (``CadenceMatrix.table``)."""

    return matrix.table(label or "", compact=bool(compact))


def data_table(
    context: Any,
    table: Annotated[Any, Describe("A `DataTable`: its columns and its rows, or its groups of rows.")],
    *,
    empty: Annotated[str, Describe("With no rows, what is missing.")] = "",
) -> dict[str, Any]:
    """A table drawn whole from its declaration, so a page writes no heading
    or cell of its own."""

    if not isinstance(table, DataTable):
        raise TypeError(f"data_table draws a DataTable, and was handed {table!r}")
    return drawn(table, context, empty=empty)


TABLE_HEADING = Part(
    name="",
    rules="""
    Each column that may leave has a rank, 1 first (`TableColumn(drop=N)`). A table that declares none
    has its order derived by the region (`table_layout.leaving_order`): `optional-col` first, then
    from the far end. It never lets go the column that names the row, one marked `key-col`, the
    closing figure of a table that marks none, a row's state or its controls; the region turns the
    ranks into the widths each leaves at.

    **Every table sorts.** A list its server sorts has links in its headings (`TableColumn(sort=)`). Every other
    heading has the same control, looking the same, drawn as a button, which the region keeps only
    where the table holds two rows to put in order, counted across its groups. A column of controls
    and a heading named for assistive technology alone have nothing to order, and
    `DataTable(keeps_order=True)` keeps a table's own order, as a list page does with the one page of its
    server's order it holds and a change table does with a change. `tables.js` sorts the rows on
    the page: ascending, descending, then as they came. Amounts, dates and lengths of time sort as
    what they are; a cell may say its own value with `data-sort`. Sections keep their own rows.
    *Gate:* `test_any_table_sorts_by_a_heading`.

    Rows selected together (`data-selectable-table`) start with a column of tick-boxes: the heading
    that selects every row, then each row's, keyed by `Row(key=)` (`DataTable(selectable=True)`).
    """,
    classes=("heading-words", "sort-indicator", "table-sort-link"),
    about="One column heading of a table: its kind, the rank it leaves by and its sort control.",
    partial="partials/_table_heading.html",
    function=table_heading,
    example=tags.TABLE_HEADING,
)
ROW_GROUP = Part(
    name="",
    rules="""
    Rows that belong to sections share one table as a `tbody` per group, headed by
    its `Group`'s label and count, which spans every column the headings declare. They are never
    two tables kept aligned by hand.

    Rows that belong under one row name it (`data-folded`) and show while it is open
    (`data-folds`, its toggle in the same gutter as the row toggle); with no script they all show.
    A group folded away is `Group(folds=<id>)`, its rows naming that id.
    """,
    classes=("row-group",),
    about="The heading row of one group of a table's rows, first in its `tbody`: its name and how many rows.",
    partial="partials/_row_group.html",
    example=tags.ROW_GROUP,
)
ROW_ACTIONS = Part(
    name="",
    rules="""
    A row reads from its first line: the toggle stands there, so every cell is set at the top, and no
    page sets its table's cells otherwise. What can be done to a row's record ends the row as
    `Row(actions=)`, under a heading of kind `control`. Each is a `PageAction` drawn as a
    compact control with its middle on the first line's. A page never draws a button in a cell
    (*Gate:* the `cell` shape). A row's controls stand side by side on one line,
    which their column asks for, and the words beside them wrap first; in a narrow box they take the
    row's last line. Every row's controls end on one edge, and each draws its edge of its own but the
    last, which is quiet only when it sets the row aside (`put_away`, a button). A cell of the row's
    actions is a control whatever its heading says. A sentence in a control cell (what a write gap
    links) is long, so it keeps a measure of `--row-words` as any long value does.

    **A row's controls stand in the row.** The rows' actions, or a column of kind `control`, stand at
    every width in the row, every one in the row's cell, with its order and mark, side by side on one
    line, all one size. A label keeps one line, uncut. A field a reader fills in (a select) is the
    row's value, so a column holding one never leaves, and in a cell it fills its cell and gives way
    with its column. The reader's way to set a row aside (`Row(put_away=)`, the queue's Dismiss) is the
    last of its buttons, drawn quiet; alone on a row's line its words start at the name's edge.
    *Gates:* the survey's `row actions beside squeezed words`, and `test_row_controls.py`.

    A row's own arrangement (a favorite's star, the pair that moves it) is no part of the cell that
    names the row: `Row(mark=)` and `Row(order=)` draw it first in the row's controls column, the star last of
    the two, a `RowMark` and a `RowOrder` (`application.pages`), so the table sizes and counts it and
    the name keeps its column's whole line.
    """,
    about="The cell that ends a row with what can be done to its record, each a compact control.",
    partial="partials/_row_actions.html",
    example=tags.ROW_ACTIONS,
    public_classes=("row-actions",),
    states=tags.ROW_ACTIONS_STATES,
)
ROW_SELECT = Part(
    name="",
    rules="""
    A cell of fixed width, so selecting moves nothing. A row with no key keeps the cell empty.
    """,
    example=tags.ROW_SELECT,
    about="The cell that holds a row's tick-box, first in a row under the heading that selects every row.",
    partial="partials/_row_select.html",
    classes=("select-column",),
)
DATA_TABLE = Part(
    name="data_table",
    about="A table drawn whole from its declaration: its columns' headings and a row of cells for each record.",
    rules="""
    **A table fits its box, at every width.** No screen width is asked anywhere in the table
    primitive, and nothing is measured: a table is a grid of its columns' tracks, each row a subgrid
    of them, the same with script and without, so every cell stands under its column's heading and a
    column is one width down the table. No width is computed in Python. Its columns are sized as
    Primer's DataTable sizes its own (`auto` beside `grow`): each column starts at the least its cells
    can wrap to, every column grows alike toward its values whole, and the room left past every
    column's values goes to the column that names the rows, which is `auto` where every other is
    `fit-content(100%)` (tables.css). The grid has a track for each of `table_layout.TRACKS` columns
    after a row's tick-box, a row's controls among them, and a table that declares more is refused,
    so no column is drawn over another. A value that keeps its line (a status, a figure, a row's
    controls, a date) starts whole. A short value (`cell-short`, `table_layout.SHORT`: an address and
    its port, a host, a mailbox, a project's name) starts whole up to its column's share, its region's
    width over one more than the count of columns it shows (`--share`, over the count the region
    writes, `data-columns`: its headings, or in a table with none its widest row; a column with
    nothing in any row, one ranked to leave at the region's width, or a `wide-only` one in a narrow
    box, is not counted), so the columns together never start wider than the region. A longer value
    starts at its longest word or piece, so it gives way first: a short value is whole wherever the
    table has room for it, and wraps (an identifier at its seams, words at their spaces) only where
    the table has none. A long value is held to its third line (below). A heading is one of its
    column's values, held as a short value is. *Gate:* the survey's `room unshared`, which fails a
    cell or a heading squeezed below what it needs while another column holds the room it lacks.

    The region turns a table's ranks (`TableColumn(drop=)`) into at most four levels from the widest
    (`data-drop`, `table_layout`): its first rank leaves while the region is narrower than 64rem, its
    next below 56rem, then 48rem and 40rem, by four container-query rules written once in tables.css,
    and any rank past the fourth leaves with it. So a narrow box holds only the columns a row keeps.
    Nothing is measured, so a column may leave a box that could still hold it. What leaves shows on
    the row's open line. The gates fail a table that runs past its card.

    A column its headings rank none for leaves by the rule `leaving_order` follows, after every
    declared rank and only at the narrowest widths: its far column at the widest of them and its
    nearest below 40rem, so a table keeps every column until a narrow box and a hand-written table
    needs no rank of its own to fit a phone. A column holding a button stands,
    since a cell held back holds none. This is the column-priority pattern of jQuery Mobile's and
    Tablesaw's column toggle (six priorities, each shown from a set width, the columns with none
    persisting) rather than their reflow mode, which stacks each cell under its label: at every width
    a table is a table, its naming column and the columns it keeps side by side under their headings.
    Below 36rem the table is in a narrow box (below).

    What is marked `narrow-only` shows in a narrow box alone: a few words in the cell that names the
    row, standing in for the columns that left (the audit log's action under its name). What is
    marked `wide-only` leaves a narrow box outright, held for no toggle, since what it says is on a
    page of its own.

    A table of marks (`data-marks` on the table: one row a thing, one column a period, a mark in each
    cell, as the cadence matrix is) centers every heading but the names over its column, and its
    cells stand closer in a narrow box and where it shares a row with a chart
    (`data-marks="compact"`). Its older periods are `wide-only`, so a narrow box keeps the newest
    three.

    The region marks each body cell that says nothing (`data-blank`) and every cell of a column blank
    in every row (`data-blank-column`), which is not drawn. Each body cell carries its column's kind
    (`data-kind`), so a figure aligns under its heading on fixed digit widths and a value is cut by
    its kind, with nothing written on the cell by hand.

    Since a table fits, its region is no scroll box and its heading pins under the page's chrome (a
    scroll box in either axis would pin it to the region). Only a region declared a pane, or a
    chart's (`chart=True`), scrolls, and pins its heading to its own edge. A region that scrolls takes
    focus, named by its table's label so the stop says what it holds, as the focusable scroll box of
    the usual responsive-table pattern is; a region that fits is a plain box. The stop is a `group`
    where that pattern writes `region`, so it is no landmark: the section or card around a table is
    one already, often by the same name. *Gates:*
    `test_a_region_that_scrolls_is_a_stop_named_for_its_table`,
    `test_a_section_and_the_table_in_it_are_one_landmark`. A pane (`pane=True`) is as tall as its rows
    up to a fixed reading height, as the map's ledger is, and scrolls past it. A table is never
    swiped through: in a narrow box its ranked columns have left. A column of words every row must
    show, such as what a change sets (its After; its Before is spare and leaves first), is a
    `key-col`, which takes no rank and never leaves, nor does the closing figure of a table that marks
    none: a table whose kept columns cannot stand at a phone's width declares too many keys.

    A key/value table (row headers, no `thead`) sizes its label column to its longest label.

    Every row is ruled from the next, a row group's heading included; only the table's last row and a
    row whose open line follows it draw no rule, since the card's edge or the open line ends them. A
    row's open line holds what left the row: its list of columns (`.row-columns`) is drawn only while
    one of its entries is, so a row with nothing gone opens to its controls alone and no empty box.
    *Gates:* the survey's `rows unruled` and `empty panels`.

    **A narrow box keeps its rows under their headings.** Below 36rem of its region (the
    `table-region` container query in tables.css) every column a row can spare has left for the row's
    open line, and what stands is the naming column, each `key-col` (or the closing figure), a row's
    state (its pills, a date, or any column under a heading of kind `status` that declares no rank
    in a table that ranks its others) and its controls, each under its heading. A column whose rows
    each say a moment is a date (`date-col`, which `DataTable` writes), never ranked to leave unless
    declared. A column of a row's choices (a field it fills in in each row, or the words standing
    where one cannot be, as the agent rules draw a column per agent) stands each choice on a line of
    its own beneath the row's name, named by its column (`data-field`), so the row keeps every one
    however many columns there are. A heading of kind `status` that declares a
    rank is one of a row's secondary checks: it leaves at that rank, whatever its cells hold, and no
    later than the table's last ranked column, so a row's own state is declared with no rank and its
    checks with one. In a narrow box a pill is as wide as its words up to its column's share, wraps
    between them past it, and is never narrower than its longest word, so a row's name is whole before
    its state is. Below every rank's width (20rem) the state goes to the open line last; a key never
    does. A hash or key with no break in it may break anywhere (cuts.css).

    A table's headings, cells, open lines and group headings keep their roles in its grid (table,
    row, column header, cell), and its heading pins. A narrow table whose rows
    hold labeled controls (below 48rem where a row holds four, which ask a line of their own sooner)
    gives them a line of their own: a row's labeled controls take its last line, on
    a line of their own from where its name starts, in every row alike, the row's pad under them,
    wrapping only between whole controls. A row of icon controls (a star, the order arrows) keeps them
    at its end on its first line. *Gates:* the survey's `tables` (each cell a row shows stands under
    its column's heading, and no row draws a column's label), `rows unruled` (a row's bottom edge is
    ruled, whichever cell draws it), `overlaps` (what two boxes draw never crosses) and `row state
    kept` (a key or a state never leaves early, and a field its row fills in is never hidden).

    **Opening a row moves no column.** An open row keeps its cells as they were, but for a clamp,
    which it releases in place, and, in a narrow box, its `narrow-more` parts, which it shows in
    place, each on a line of its own that asks its column for no width. What it holds back stands on
    the line drawn after it (`tr.row-detail`), whose content asks its columns for no width: the blocks
    the server moves there, then an entry for each cell that holds something back, under its column's
    name. So every column keeps its edges and every other row its height. *Gate:*
    `test_opening_a_row_moves_no_column_and_no_other_row`.

    A closed fold's rows are hidden and no height, and still size their columns, so opening it moves
    no column (*Gate:* the layout gate's `opens_every_fold`). So their controls would widen a column
    shared down the table for every row, and a table of one column with no headings, whose rows each
    say one thing (the queue), may size each row's controls apart (`DataTable(controls_per_row=True)`): in a box of 48rem or more
    each row is a grid of its words and its own controls at its end, and opening a fold moves no other
    row's words or controls. *Gate:* `test_opening_a_fold_moves_no_other_rows_controls`.

    The server writes each cell once. A cell cannot also stand on a line beneath its row, so
    tables.js copies what the row holds back onto that line as the row first opens (or as it arrives
    drawn open), from the one frame the region draws (`template[data-open-line]`): each cell of a
    ranked column, shown where its column has left (`data-drop`), and each cell holding a
    part only the open line shows (`row-more`, `line-rest`), shown always, what the row's line already
    shows of it only once its column has left (`data-row-shows`). A copy draws no id twice. A cell
    held back holds no control, which a copy would draw twice: the region refuses one, so a button
    stands in place or moves whole (`row-below`). A row's open line a script adds is
    `{% open_line %}`. *Gates:*
    `test_table_once.py`: `test_a_row_writes_each_cell_once`, `test_a_cell_held_back_holds_no_control`,
    and the byte budgets `ROW_BYTES` and `CHROME_BYTES`.

    - A `row-below` block, which the row's line never shows, and the row's editor are moved into a
      line the server draws after the row (`tr.row-detail`, `drawn_tables`), shown while the
      row is open: the blocks first, then each labeled block under its label. A row holding a block
      says so on its `tr` (`data-below`), which shows its toggle. A block's stacked parts are spaced
      by their list's gap, with the cell's normal pad after the last part.
    - A table whose line is the rest of what a row says and does (`data-line="flat"` on the table)
      draws it flat: one column on no panel, the buttons of its parts side by side, and on a phone
      each the line's width, one to a line.
    - The browser's find does not reach into a closed row: what it holds back is not drawn until the
      row opens.
    - A part that reads differently on the row's line and on the line beneath asks where it stands
      (`@container style(--cell: line)`, or `open`), which the table primitive sets on its cells, and
      never names the table: an ask's note says its word alone on the row's line and its sentence
      beneath.

    A row's opening is its only disclosure. What is shown only once a row is open is marked
    `.row-more` (a word, a list item), or `.row-below` for a block, which opens on the line beneath
    the row at the table's width. Never a `<details>` inside a cell.

    **A table is laid out before any script runs.** Nothing is measured to lay a table out:
    `tables.js` only sorts, selects and opens rows, and loads deferred (*Gate:*
    `test_the_table_script_lays_nothing_out`). No table is drawn by hand, an extension's included:
    a table is a `DataTable` its view declares, and the `cell` shape of `hand_drawn.SHAPES` refuses
    a `<td>` or a `<th>` in any template of the host or an extension but the table's own partials,
    as it loads. *Gate:* the `cell` shape.

    A long value in a cell (a line past `table_layout.LONG`) wraps, and in a narrow box is held to its
    third line by a clamp with its mark (`.cell-clamp`, cuts.css), its whole value where its row
    opens; a wide box holds it whole. The name a row is read by is never held, nor is a row with no
    toggle (a table with no headings, a fold's head), so their values wrap whole, and no cell carries
    its value again as a title. Only words are held: each part of words in it (`data-cell`) is clamped
    on its own, so a pill or a toggle beside them stays whole, and a cell holding such parts is the
    clamp itself, with no box drawn around them. A value of words of 32 characters at most
    (`table_layout.SHORT`: an address and its port, a host, a project's name) is one box (`cell-short`,
    the value's own list where it is one) as wide as the value up to its column's share, and as wide
    as its column where that is more, so its column holds it whole while the table is laid out as
    one, and past its share it wraps inside its box. Its column still asks for the value whole
    (`calc-size` holds only the share as its floor), so room the table has past every column's needs
    goes to it; a longer one wraps at its words in a line of `--row-words`, or its share where that is
    less, as a long one does; a longer identifier and a row's own name break anywhere their column
    lacks room; a pill and prose break only inside a word wider than their column, a pill longer than
    its column wrapping inside its one box. A status, a figure, a date or a control is never cut: its
    column is as wide as it is.

    An identifier the row is read by, in a row of many columns (a container's image), is
    `code.cell-whole`, which the clamp never holds. A value told in several parts marks each
    `cell-part`.

    What each part of a cell is decides how it wraps, and the region marks it (`data-cell`,
    `table_layout`). The stylesheet keys its cuts, holds and wraps on that mark, never on a
    page's class or a cell's tags. The row's `name` is the value of the column that names it, words or
    a link to its record's page, drawn bold whether or not it links, which no table declares; an
    identifier drawn as code, a sentence (`role="prose"`), a muted note, a moment or a state there names nothing. Free text (a message, a record's words) is declared `Cell(role="data")` and marked `data`;
    what a row means in words is `Cell(role="prose")`. A context line is a muted cell. A table declared
    later sets as every other does by declaring those, and `lint:css` refuses a table primitive rule
    that tells a cell's part by anything else. Free text breaks a run with no seam in it (a link, a
    digest) wherever it must, so it never holds its column wider than its box. An identifier in a
    prose column waits for the open row (`row-more`) rather than holding that column at its cap.

    A run of links longer than the count its template declares folds behind "+N", and a stacked list
    longer than four shows three and "+N more". A row that holds anything back gets one toggle that
    lifts every cut and opens every fold in it.

    An open row and the line beneath it hold every box in them to its cell, in one rule of the table
    primitive (`surfaces.css`). No box asks for a width of its own or grows wider than what holds it,
    and a chip is a box, its padding inside that width. So nothing there runs past or over a cell at
    any width, whatever a page's own rule says, and no page sizes a box inside a table. `lint:css`
    refuses a width, an inline size, a flex basis or a grid's columns on a selector that reaches
    inside one (`.data-table` and a combinator, a cell, `.row-detail`, `.row-columns`). Only the table
    primitive's sheets may set them (`base.css`, `cuts.css`, `figures.css`, `tables.css`,
    `surfaces.css`). *Gates:* `cells` and `cell labels` in the layout survey, read with rows open by
    `dev:look --expand`; `lint:css`.

    An intrinsic size (`max-content`, `min-content`) goes on an element inside a cell, never on the
    `td`. Chromium ignores it on a table cell and WebKit does not, so the two engines would size the
    column differently.

    An identifier short enough to hold its line is never broken while every column stands; once
    columns leave for the open line, where it is drawn whole, its seams are where it breaks. The row's
    own name, free text, a preview and a context line wrap as they always do. *Gate:* `broken
    identifiers` in the layout survey,
    `test_an_identifier_in_a_table_is_whole_while_a_column_can_leave`.

    A box set in a line that cuts what it holds (an inline block whose overflow is not visible) takes
    its bottom edge as its baseline, so its words ride above the line's. So it is set by its top
    (`vertical-align: top`).

    A pill is as wide as its words wherever it stands, in a cell alone or beside words, and is one
    box: a pill longer than its column wraps inside its own paint, never into two pills. *Gates:* the
    survey's `pills in pieces`, `a_table_fits_its_box_at_every_width`,
    `test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split`, the survey's `sideways`, and the
    page crawl, which fails any `.data-table` outside `.table-scroll`.

    A region whose tables hold no row draws the one empty state (`partials/_empty_state.html`) in their
    place, so no page draws headings over nothing. Its `empty=` says what is missing in the page's
    words, with `remedy=` (or `action_url=` and `action_label=`). `filtered=` with `clear=` says its
    filter emptied it, with the way back. A table whose rows open an editor (`Cell(editor=)`) is a
    region of the page's own (`data-fragment`, its id from its label), so an editor opens, saves and
    closes in place.
    """,
    partial="partials/_data_table.html",
    function=data_table,
    marked_in=table_layout,
    public=True,
    example=tags.DATA_TABLE,
    states=tags.DATA_TABLE_STATES,
    # Its box, a row that is open, and its open line.
    classes=("data-table", "is-expanded", "is-pane", "row-columns", "row-detail", "table-scroll"),
)
slotted(DATA_TABLE, library=register)
ROW_TOGGLE = Part(
    name="",
    rules="""
    Every row that can open has its toggle drawn by the server. A table with headings whose rows can
    hold anything back (a column that leaves, by its rank or the region's, a long value a narrow box
    clamps, a part only the open line shows, a block moved there, an editor or a fold) keeps the
    gutter in the column that names each row: the region marks that column's cells `row-gutter` and
    draws each row's toggle last in its cell, so no table can hide a value without the way to read
    it whole; a table with no headings keeps it in its first column. It shows only where opening the row shows something now: a cell whose column has left, a
    `narrow-more` part in a narrow box, a `row-below` part, a part the row never shows, a clamp in a
    narrow box, or a fold. A wide box clamps nothing, so a row owes no toggle there for a long
    value.

    The toggle stands in a gutter that every row of the table keeps while any row opens, so names
    share one edge whether or not a given row opens. The gutter follows from the server's markup and
    the box's width alone.
    """,
    classes=("row-expand",),
    about="The toggle that opens a row to all it holds, last in the cell that names it, in a table with no headings.",
    partial="partials/_row_toggle.html",
    example=tags.ROW_TOGGLE,
)
