"""The tags a data table is declared through.

``{% table_region %}`` wraps the table in its labeled scroll region,
``{% table_heading %}`` draws each column heading with its kind, its drop
rank and its sort control, and ``{% row_toggle %}`` draws the disclosure of a
row that holds more than it shows. All of it is in the HTML on first paint, so
nothing about a table's shape waits on a script: the region marks each cell
with what its column does and the width its column leaves below, derived from
what the column holds (`tables.fit`); the stylesheet drops a column there, and
shows a row's toggle where the row has something hidden.
"""

import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import wraps
from html.parser import HTMLParser
from typing import Any

from django import template
from django.template.base import token_kwargs
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from hq.platform.application.tables import (
    CUT,
    NAME_HOLD,
    Fit,
    FitColumn,
    check_drop,
    column_floor,
    column_kind,
    fit,
)

register = template.Library()

# A cell that says nothing: dashes and space, and nothing drawn but the empty
# value's own mark.
_SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")


_SECTIONS = ("thead", "tbody", "tfoot")

# The characters a cell of prose or a status holds out for (`data-long`,
# `css/app/tables.css`), and the line that holds out for it: one that would
# stand three lines tall at the floor. A shorter line wraps as it likes, since
# held to the floor its column could stand wider than all it says.
_FLOOR = 16
_LONG = 3 * _FLOOR

# What a cell holds that is not on its row's line: words for assistive
# technology alone, and what the row keeps for its open line or shows only in
# a narrow box.
_OFF_LINE = frozenset({"visually-hidden", "row-below", "row-more", "narrow-only"})
# Text that breaks wherever it must, and a part of a phrase, which holds out
# for none of its length (figures.css): no run of either holds its column open.
_HOLDS_NOTHING = frozenset({"cell-data", "part"})
# A column of tick-boxes is as wide as `.select-column` (tables.css).
_TICK_BOXES = 38

_TAG_NAME = re.compile(r"<\w+")
# What the open elements say of a value the stylesheet cuts: an entity's name,
# a token that is the whole value, a box cut inside.
_CUTS = frozenset({"[data-entity]", "[data-sole]", "cut"})

# The tags that end a line of a cell.
_LINE_ENDS = frozenset({"br", "div", "p", "li", "dt", "dd"})


@dataclass(slots=True)
class _Cell:
    start: tuple[int, int]
    attrs: dict[str, str | None]
    # Where its content starts (past the start tag) and, when the cell closes
    # with its own end tag, where that tag is.
    opened: tuple[int, int]
    closed: tuple[int, int] | None = None
    says_nothing: bool = True
    # The characters of text on its line so far, and on its longest line,
    # spaces collapsed.
    line: int = 0
    longest: int = 0
    # The characters of the run with no break in it so far, the most of it
    # that holds the column open (None: all of it), and the longest run held:
    # a line breaks at a space and at a name's seams.
    run: int = 0
    hold: int | None = None
    widest: int = 0

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(_classes(self.attrs))

    def say(self, text: str, hold: int | None) -> None:
        self.line += len(" ".join(text.split()))
        self.longest = max(self.longest, self.line)
        for place, run in enumerate(re.split(r"\s+", text)):
            if place:
                self.end_run()
            if run:
                if not self.run:
                    self.hold = hold
                elif hold is not None:
                    self.hold = hold if self.hold is None else min(self.hold, hold)
                self.run += len(run)

    def end_run(self) -> None:
        self.widest = max(self.widest, self.run if self.hold is None else min(self.run, self.hold))
        self.run, self.hold = 0, None

    def end_line(self) -> None:
        self.end_run()
        self.line = 0


@dataclass(slots=True)
class _Row:
    section: str
    group: bool
    cells: list[_Cell] = field(default_factory=list)


class _Cells(HTMLParser):
    """Each cell of a region's own tables, by its row and its place in the row,
    and what the stylesheet is to read off it. Only the region's own tables: a
    table inside a cell is that cell's content, and says something."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables = 0
        # Where the region's own table starts.
        self.table: tuple[int, int] = (0, 0)
        self.section = ""
        self.rows: list[_Row] = []
        self.cell: _Cell | None = None
        # The elements open inside the cell, each its tag, and its classes and
        # whether it is an entity's name.
        self.open: list[tuple[str, frozenset[str]]] = []
        # Where each cell starts, with what is written into it there.
        self.marks: dict[tuple[int, int], list[str]] = {}
        # The content of each cell that keeps its line, from where it starts
        # to its cell's end tag.
        self.held: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        own = self.tables == 1
        if self.cell is not None and own and tag in ("td", "th", "tr", *_SECTIONS):
            self._close()
        if self.cell is not None:
            self._inside(tag, attrs)
            if tag == "table":
                self.tables += 1
            return
        if tag == "table":
            self.tables += 1
            if self.tables == 1:
                self.rows, self.section, self.table = [], "", self.getpos()
        elif own and tag in _SECTIONS:
            self.section = tag
        elif own and tag == "tr":
            self.rows.append(_Row(self.section, "row-group" in _classes(dict(attrs))))
        elif own and tag in ("td", "th") and self.rows:
            line, column = self.getpos()
            tag_text = self.get_starttag_text() or ""
            # The start tag can run over lines: its content starts where its text ends.
            *above, last = tag_text.split("\n")
            opened = (line + len(above), (0 if above else column) + len(last))
            self.cell = _Cell((line, column), dict(attrs), opened)
            self.rows[-1].cells.append(self.cell)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cell is not None:
            self._inside(tag, attrs, void=True)

    def _inside(self, tag: str, attrs: list[tuple[str, str | None]], *, void: bool = False) -> None:
        assert self.cell is not None
        named = dict(attrs)
        classes = frozenset(_classes(named))
        if tag in _LINE_ENDS:
            self.cell.end_line()
        elif "seam" in classes:
            self.cell.end_run()
        if tag != "br" and "empty-value" not in classes and not self._within({"empty-value"}):
            self.cell.says_nothing = False
        if not void and tag not in _VOID:
            marks = {f"[{name}]" for name in ("data-entity", "data-sole") if name in named}
            self.open.append((tag, classes | marks))

    def _within(self, names: Iterable[str]) -> bool:
        assert self.cell is not None
        return any(classes.intersection(names) for classes in (self.cell.classes, *(c for _t, c in self.open)))

    def _hold(self) -> int | None:
        """How much of a run with no break in it holds its column open here:
        all of an identifier shown whole (`cell-whole`), none of text that
        breaks anywhere or of a phrase's part, the first characters of a value
        the stylesheet cuts, and of anything else a name's hold."""

        if self._within({"cell-whole"}):
            return None
        if self._within(_HOLDS_NOTHING):
            return 0
        if self._within(_CUTS) or any(tag == "code" for tag, _classes in self.open):
            return CUT
        return NAME_HOLD

    def handle_endtag(self, tag: str) -> None:
        if self.cell is not None:
            for depth in range(len(self.open) - 1, -1, -1):
                if self.open[depth][0] == tag:
                    del self.open[depth:]
                    break
        if tag == "table":
            if self.tables == 1:
                if self.cell is not None:
                    self._close()
                self._mark_table()
            self.tables -= 1
        elif self.tables == 1 and self.cell is not None and tag in ("td", "th", "tr", *_SECTIONS):
            if tag in ("td", "th"):
                self.cell.closed = self.getpos()
            self._close()
        if self.tables == 1 and tag in _SECTIONS:
            self.section = ""

    def handle_data(self, data: str) -> None:
        if self.cell is None or self._within({"empty-value"}):
            return
        if not self._within(_OFF_LINE):
            self.cell.say(data, self._hold())
        if not _SAYS_NOTHING.fullmatch(data):
            self.cell.says_nothing = False

    def _close(self) -> None:
        if self.cell is not None:
            self.cell.end_run()
        self.cell = None
        self.open = []

    def _mark_table(self) -> None:
        """Each cell says what its column does, so the stylesheet reads it off
        the cell itself and never matches a cell to its heading by place."""

        heads = [row.cells for row in self.rows if row.section == "thead"]
        kinds = _kinds(heads)
        lines = {index: kind for index, kind in kinds.items() if kind in ("status", "numeric")}
        named = _naming_column(heads)
        records = [row for row in self.rows if row.section == "tbody" and not row.group]
        empty = _empty_columns(heads, named, records)
        width = _width(heads, records)
        table = fit(_fit_columns(heads, records, named, empty, width))
        self.marks[self.table] = _table_marks(table, heads)
        for row in self.rows:
            if row.group:
                self._span(row, width)
                continue
            body = row.section == "tbody"
            for index, cell in enumerate(row.cells):
                marks = [" data-blank"] if body and cell.says_nothing else []
                if "colspan" not in cell.attrs:
                    marks += _column_marks(
                        cell, table.leaves.get(index), lines.get(index) if body else None, index in empty
                    )
                    if body and index in lines and cell.closed and not cell.says_nothing:
                        self.held.append((cell.opened, cell.closed))
                    if body and index != named and kinds.get(index) in ("prose", "status") and cell.longest > _LONG:
                        marks.append(" data-long")
                if marks:
                    self.marks[cell.start] = marks

    def _span(self, row: _Row, width: int) -> None:
        """A group's heading row spans every column its table declares."""

        for cell in row.cells:
            if width and "colspan" not in cell.attrs:
                self.marks[cell.start] = [f' colspan="{width}"']


def _classes(attrs: dict[str, str | None]) -> list[str]:
    return (attrs.get("class") or "").split()


def _fit_columns(
    heads: list[list[_Cell]], body: list[_Row], named: int, empty: set[int], width: int
) -> tuple[FitColumn, ...]:
    """What the table's fit reads of each column, by place: its floor, from
    its kind, its longest line and run and its heading's label, and its rank.
    Without headings, a column of row headers keeps its line and any other is
    prose."""

    headings = {index: cell for cells in heads for index, cell in enumerate(cells)}
    if rank := _rank(headings.get(named)):
        raise ValueError(f"The column that names each row has no drop rank, not {rank}.")
    rows = [row.cells for row in body if not any("colspan" in cell.attrs for cell in row.cells)]
    columns = []
    for index in range(width):
        heading = headings.get(index)
        if index in empty:
            columns.append(FitColumn(0, shares=False))
            continue
        if heading is not None and "select-column" in heading.classes:
            columns.append(FitColumn(_TICK_BOXES, shares=False))
            continue
        cells = [cells[index] for cells in rows if index < len(cells)]
        headers = bool(cells) and all(cell.attrs.get("scope") == "row" for cell in cells)
        floor = column_floor(
            (heading.attrs.get("data-kind") if heading else None) or ("status" if headers else "prose"),
            max((cell.longest for cell in cells), default=0),
            max((cell.widest for cell in cells), default=0),
            heading.longest if heading else 0,
            sorts=heading is not None and "aria-sort" in heading.attrs,
            names_row=index == named,
            gutter=index == named and any("row-gutter" in cell.classes for cell in cells),
        )
        columns.append(FitColumn(floor, _rank(heading)))
    return tuple(columns)


def _rank(heading: _Cell | None) -> int:
    return int(heading.attrs.get("data-drop") or 0) if heading else 0


def _table_marks(table: Fit, heads: list[list[_Cell]]) -> list[str]:
    """What the table says of its fit: the width below which it is in a
    narrow box, and, where it declares its columns, how many share its width,
    then how many below each width a column leaves at (``800:4``)."""

    marks = [f' data-narrow-at="{table.narrow}"']
    if heads:
        counts = [str(table.columns), *(f"{width}:{count}" for width, count in table.columns_below)]
        marks.append(f' data-columns="{" ".join(counts)}"')
    return marks


def _column_marks(cell: _Cell, leaves: int | None, kind: str | None, blank_column: bool) -> list[str]:
    """What a cell says of its column: the width it leaves below, the line it
    keeps, and whether the whole column is blank. A mark the template wrote
    stands."""

    marks = []
    if leaves and "data-drop-at" not in cell.attrs:
        marks.append(f' data-drop-at="{leaves}"')
    if kind and "data-kind" not in cell.attrs:
        marks.append(f' data-kind="{kind}"')
    if blank_column:
        marks.append(" data-blank-column")
    return marks


def _kinds(heads: list[list[_Cell]]) -> dict[int, str]:
    """Each column's kind, by place, from the heading that declares it: the
    cells of a column that keeps its line (a status, a figure) say so too,
    and a long cell of prose or a status holds out for a floor."""

    return {index: kind for cells in heads for index, cell in enumerate(cells) if (kind := cell.attrs.get("data-kind"))}


def _width(heads: list[list[_Cell]], body: list[_Row]) -> int:
    """How many columns the headings declare, or in a table with none its widest
    row: what a group's heading row spans."""

    rows = heads or [row.cells for row in body]
    return max((sum(int(cell.attrs.get("colspan") or 1) for cell in cells) for cells in rows), default=0)


def _naming_column(heads: list[list[_Cell]]) -> int:
    """The place of the column that names the row: the first, or the second
    after a column of tick-boxes."""

    return int(any(cells and "select-column" in _classes(cells[0].attrs) for cells in heads))


def _empty_columns(heads: list[list[_Cell]], named: int, body: list[_Row]) -> set[int]:
    """The columns with a heading and nothing in any row. Never the column that
    names the row."""

    width = max((len(cells) for cells in heads), default=0)
    empty = set()
    for index in range(named + 1, width):
        cells = [row.cells[index] for row in body if index < len(row.cells)]
        if cells and all(cell.says_nothing for cell in cells):
            empty.add(index)
    return empty


_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


def mark_cells(markup: str) -> str:
    """The region's tables with each table and cell marked for what the
    stylesheet reads: the table's fit (``data-narrow-at``, ``data-columns``);
    a body cell that says nothing (``data-blank``), so it is not counted as
    something a row holds back; a cell of a column that leaves below a width
    (``data-drop-at``); a cell of a column blank in every row
    (``data-blank-column``), which is not drawn; a cell of prose or a status
    longer than its floor (``data-long``); and a group's heading row spanning
    every column its headings declare (``colspan``). What a cell of a column
    that keeps its line says is one ``.kept-line``, so the stylesheet can hold
    the line whole up to the column's share of its region and let it wrap past
    that."""

    cells = _Cells()
    cells.feed(markup)
    cells.close()
    if not cells.marks and not cells.held:
        return markup
    # The parser counts lines by newline alone.
    lines = [0] + [found.end() for found in re.finditer("\n", markup)]

    def at(row: int, column: int) -> int:
        return lines[row - 1] + column

    # The marks go straight after the tag's name: `<td`, `<th` or `<table`.
    inserts = [(_TAG_NAME.match(markup, at(*start)).end(), "".join(marks)) for start, marks in cells.marks.items()]
    for opened, closed in cells.held:
        inserts += [(at(*opened), '<span class="kept-line">'), (at(*closed), "</span>")]
    parts, last = [], 0
    for offset, text in sorted(inserts, key=lambda insert: insert[0]):
        parts += [markup[last:offset], text]
        last = offset
    parts.append(markup[last:])
    return "".join(parts)


# The shares of its region an identifier may take, besides the whole row's
# (`css/app/tables.css`, `--identifier-max`).
_ROOMS = frozenset({"", "shared", "snug"})


def _region(
    label: str,
    *,
    css: str = "",
    chart: bool = False,
    pane: bool = False,
    room: str = "",
    mid: Any = None,
    narrow: Any = None,
) -> dict[str, Any]:
    """The labeled region a table sits in: the container its columns leave by.
    An identifier in it is cut at a share of the region's width; ``room``
    names a smaller share for a row whose identifier sits beside wide columns
    (``shared``) or beside other identifiers (``snug``).

    A table fits its region, so the region is no scroll box and its heading
    pins under the page's chrome. Only a ``pane`` (a box of its own height, or
    a table sized to its content) and a ``chart``'s table scroll in it, and
    those are reachable from the keyboard."""

    # TODO(note-hq-chokepoints-and-tables-plan, wave 1 item 6): ``mid`` and
    # ``narrow`` are accepted and ignored until no template passes them.
    del mid, narrow
    if room not in _ROOMS:
        raise ValueError(f"A region's room is one of {sorted(_ROOMS)}, not {room!r}.")
    classes = [
        "table-scroll",
        *([css] if css else []),
        *(["is-pane"] if pane else []),
        *([f"is-{room}"] if room else []),
    ]
    return {"label": label, "css": " ".join(classes), "chart": chart, "scrolls": pane or chart}


class RegionNode(template.Node):
    """``{% table_region %}``: what it encloses, in its region."""

    child_nodelists = ("nodelist",)

    def __init__(self, nodelist: Any, label: Any, options: dict[str, Any]) -> None:
        self.nodelist = nodelist
        self.label = label
        self.options = options

    def render(self, context: Any) -> str:
        region = _region(
            self.label.resolve(context), **{name: value.resolve(context) for name, value in self.options.items()}
        )
        content = self.nodelist.render(context)
        return format_html(
            '<div class="{}" role="region" aria-label="{}"{}{}>{}</div>',
            region["css"],
            region["label"],
            mark_safe(' tabindex="0"') if region["scrolls"] else "",
            mark_safe(" data-chart") if region["chart"] else "",
            # The region's own content, rendered and escaped already; the mark
            # adds an attribute and nothing else.
            mark_safe(mark_cells(content)),
        )


@register.tag("table_region")
@wraps(_region)
def table_region(parser: Any, token: Any) -> RegionNode:
    bits = token.split_contents()
    options = token_kwargs(bits[2:], parser)
    try:
        if len(bits) < 2 or len(options) != len(bits) - 2:
            raise TypeError("a label, then name=value")
        inspect.signature(_region).bind(bits[1], **options)
    except TypeError as wrong:
        raise template.TemplateSyntaxError(f"table_region takes {inspect.signature(_region)}: {wrong}") from wrong
    nodelist = parser.parse(("endtable_region",))
    parser.delete_first_token()
    return RegionNode(nodelist, parser.compile_filter(bits[1]), options)


def _server_sort(context: Any, ascending: str, descending: str) -> dict[str, str]:
    """A list's own sort for this column: where the link goes and which way it stands."""

    current = context["table"]["selected_sort"]
    descending = descending or f"-{ascending}"
    next_sort = descending if current == ascending else ascending
    params = context["request"].GET.copy()
    params["sort"] = next_sort
    params.pop("page", None)
    return {
        "url": f"?{params.urlencode()}",
        "direction": "ascending" if current == ascending else "descending" if current == descending else "none",
        "next_direction": "descending" if next_sort == descending else "ascending",
    }


@register.inclusion_tag("partials/_table_heading.html", takes_context=True)
def table_heading(
    context: Any,
    label: str,
    kind: str = "",
    *,
    css: str = "",
    drop: int = 0,
    sort: str = "",
    descending: str = "",
    local_sort: bool = False,
    hidden: bool = False,
    title: str = "",
    gutter: bool = False,
    select_all: bool = False,
    note: str = "",
    note_css: str = "",
) -> dict[str, Any]:
    """One column heading.

    ``kind`` is one of ``COLUMN_KINDS``, or follows from ``css`` when left out.
    ``drop`` is the order a narrow table lets the column go in, 1 first; its
    region derives where each goes. ``sort`` names the field a
    server-sorted list orders this column by (``descending`` when its reverse is
    not ``-field``); ``local_sort`` sorts the rows already on the page instead.
    ``hidden`` keeps the label for assistive technology only, ``gutter`` makes
    room for the row toggles beneath it, and ``select_all`` draws the tick-box
    that selects every row. ``note`` is a word beside the label that qualifies
    the whole column, set in ``note_css``.
    """

    if sort and local_sort:
        raise ValueError(f"Column {label!r} sorts on the server or on the page, not both.")
    kind = "control" if select_all else column_kind(kind, css)
    check_drop(drop, css, label)
    classes = [css] if css else []
    classes += ["select-column"] if select_all else []
    classes += ["row-gutter"] if gutter else []
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
        "note": note,
        "note_css": note_css,
    }


@register.filter
def sortable(rows: Any) -> bool:
    """Whether the rows on a page are worth sorting there: two or more.
    ``local_sort=rows|sortable`` on each heading that sorts them, or the count
    of a table's rows where they come in groups (``local_sort=count|sortable``)."""

    return (rows if isinstance(rows, int) else len(rows)) > 1


@register.inclusion_tag("partials/_row_group.html")
def row_group(label: str, count: int | None = None, *, folds: str = "") -> dict[str, Any]:
    """The heading row of one group of a table's rows, first in that group's
    ``tbody``: its name and, given one, how many rows it holds. It spans every
    column, which its region counts from the table's headings, and its name
    keeps the edge of the rows' names. ``folds`` is the row's id when the group
    starts folded away: its rows name it in ``data-folded``, and its toggle
    stands in the rows' gutter."""

    return {"label": label, "count": count, "folds": folds}


@register.inclusion_tag("partials/_row_toggle.html")
def row_toggle() -> dict[str, Any]:
    """The toggle that opens a row to all it holds, last in the cell that names the
    row; that cell, in every row of the table, and its heading make the gutter.
    The stylesheet shows it only where the row has something hidden."""

    return {}
