"""The tags a data table is declared through.

``{% table_region %}`` wraps the table in its labeled scroll region,
``{% table_heading %}`` draws each column heading with its kind, the box width
it leaves at, and its sort control, and ``{% row_toggle %}`` draws the
disclosure of a row that holds more than it shows. All of it is in the HTML on
first paint, so nothing about a table's shape waits on a script: the region
marks each cell with what its column does, the stylesheet drops a column by
the tier its cells carry, and shows a row's toggle where the row has something
hidden.
"""

import inspect
import re
from dataclasses import dataclass, field
from functools import wraps
from html.parser import HTMLParser
from typing import Any

from django import template
from django.template.base import token_kwargs
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from hq.platform.application.tables import check_drop, column_kind

register = template.Library()

# Where a region's narrow and mid-width boxes end is the stylesheet's
# (`css/app/tables.css`, the table region's container queries); a heading names only the
# widest box it leaves in.
TIERS = ("mid", "narrow")
_DROPS = "_table_drops"

# A cell that says nothing: dashes and space, and nothing drawn but the empty
# value's own mark.
_SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")


_SECTIONS = ("thead", "tbody", "tfoot")

# The characters a prose cell's floor holds out for (`data-long`,
# `css/app/tables.css`), and the line that holds out for it: one that would
# stand three lines tall at the floor. A shorter line wraps as it likes, since
# held to the floor its column could stand wider than all it says.
_FLOOR = 16
_LONG = 3 * _FLOOR

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
        self.section = ""
        self.rows: list[_Row] = []
        self.cell: _Cell | None = None
        # Inside the empty value's mark: its tag and how deep.
        self.mark: list[Any] | None = None
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
                self.rows, self.section = [], ""
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
        if tag in _LINE_ENDS:
            self.cell.line = 0
        if self.mark is not None:
            if tag == self.mark[0] and not void:
                self.mark[1] += 1
            return
        if "empty-value" in _classes(dict(attrs)):
            if not void and tag not in _VOID:
                self.mark = [tag, 1]
            return
        if tag != "br":
            self.cell.says_nothing = False

    def handle_endtag(self, tag: str) -> None:
        if self.mark is not None and tag == self.mark[0]:
            self.mark[1] -= 1
            if not self.mark[1]:
                self.mark = None
            return
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
        if self.cell is None or self.mark is not None:
            return
        self.cell.line += len(" ".join(data.split()))
        self.cell.longest = max(self.cell.longest, self.cell.line)
        if not _SAYS_NOTHING.fullmatch(data):
            self.cell.says_nothing = False

    def _close(self) -> None:
        self.cell = None
        self.mark = None

    def _mark_table(self) -> None:
        """Each cell says what its column does, so the stylesheet reads it off
        the cell itself and never matches a cell to its heading by place."""

        heads = [row.cells for row in self.rows if row.section == "thead"]
        tiers = _drop_tiers(heads)
        kinds = _kinds(heads)
        lines = {index: kind for index, kind in kinds.items() if kind in ("status", "numeric")}
        named = _naming_column(heads)
        records = [row for row in self.rows if row.section == "tbody" and not row.group]
        empty = _empty_columns(heads, named, records)
        width = _width(heads, records)
        for row in self.rows:
            if row.group:
                self._span(row, width)
                continue
            body = row.section == "tbody"
            for index, cell in enumerate(row.cells):
                marks = [" data-blank"] if body and cell.says_nothing else []
                if "colspan" not in cell.attrs:
                    marks += _column_marks(cell, tiers.get(index), lines.get(index) if body else None, index in empty)
                    if body and index in lines and cell.closed and not cell.says_nothing:
                        self.held.append((cell.opened, cell.closed))
                    if body and index != named and kinds.get(index) == "prose" and cell.longest > _LONG:
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


def _drop_tiers(heads: list[list[_Cell]]) -> dict[int, str]:
    """Each column's tier, by its place, from the heading that declares it."""

    tiers: dict[int, str] = {}
    for cells in heads:
        for index, cell in enumerate(cells):
            tier = cell.attrs.get("data-drop-tier")
            if tier in TIERS:
                tiers.setdefault(index, tier)
    if 0 in tiers:
        raise ValueError("The column that names each row has no drop rank.")
    return tiers


def _column_marks(cell: _Cell, tier: str | None, kind: str | None, blank_column: bool) -> list[str]:
    """What a cell says of its column: the box it leaves in, the line it keeps,
    and whether the whole column is blank. A mark the template wrote stands."""

    marks = []
    if tier and "data-drop-tier" not in cell.attrs:
        marks.append(f' data-drop-tier="{tier}"')
    if kind and "data-kind" not in cell.attrs:
        marks.append(f' data-kind="{kind}"')
    if blank_column:
        marks.append(" data-blank-column")
    return marks


def _kinds(heads: list[list[_Cell]]) -> dict[int, str]:
    """Each column's kind, by place, from the heading that declares it: the
    cells of a column that keeps its line (a status, a figure) say so too,
    and a long cell of prose holds out for a floor."""

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
    """The region's tables with each cell marked for what the stylesheet reads:
    a body cell that says nothing (``data-blank``), so it is not counted as
    something a row holds back; a cell of a column that leaves in a narrower
    box (``data-drop-tier``, its heading's); and a cell of a column blank in
    every row (``data-blank-column``), which is not drawn; a cell of prose
    longer than its floor (``data-long``); and a group's heading row spanning
    every column its headings declare (``colspan``). What a cell of a
    column that keeps its line says is one ``.kept-line``, so the stylesheet
    can hold the line whole up to the column's share of its region and let it
    wrap past that."""

    cells = _Cells()
    cells.feed(markup)
    cells.close()
    if not cells.marks and not cells.held:
        return markup
    # The parser counts lines by newline alone.
    lines = [0] + [found.end() for found in re.finditer("\n", markup)]

    def at(row: int, column: int) -> int:
        return lines[row - 1] + column

    # `<td` or `<th`: the marks go straight after the name.
    inserts = [(at(*start) + 3, "".join(marks)) for start, marks in cells.marks.items()]
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
    mid: int = 0,
    narrow: int = 0,
) -> dict[str, Any]:
    """The labeled region a table sits in: the container its tiers are
    measured against, and how many columns, by rank, leave in a mid-width box
    (``mid``) and in a narrow one (``narrow``). An identifier in it is cut at a
    share of the region's width; ``room`` names a smaller share for a row whose
    identifier sits beside wide columns (``shared``) or beside other
    identifiers (``snug``).

    A table fits its region, so the region is no scroll box and its heading
    pins under the page's chrome. Only a ``pane`` (a box of its own height, or
    a table sized to its content) and a ``chart``'s table scroll in it, and
    those are reachable from the keyboard."""

    # A count worked out from a table's width (`columns|length|add:-2`) is none
    # below nothing.
    counts = {"mid": max(0, int(mid or 0)), "narrow": max(0, int(narrow or 0))}
    if counts["narrow"] < counts["mid"]:
        raise ValueError("A narrow box drops at least the columns a mid-width one does.")
    if room not in _ROOMS:
        raise ValueError(f"A region's room is one of {sorted(_ROOMS)}, not {room!r}.")
    classes = [
        "table-scroll",
        *([css] if css else []),
        *(["is-pane"] if pane else []),
        *([f"is-{room}"] if room else []),
    ]
    return {"label": label, "css": " ".join(classes), "chart": chart, "scrolls": pane or chart, "counts": counts}


class RegionNode(template.Node):
    """``{% table_region %}``: what it encloses, in its region, with the region's
    counts in the context its headings are drawn in."""

    child_nodelists = ("nodelist",)

    def __init__(self, nodelist: Any, label: Any, options: dict[str, Any]) -> None:
        self.nodelist = nodelist
        self.label = label
        self.options = options

    def render(self, context: Any) -> str:
        region = _region(
            self.label.resolve(context), **{name: value.resolve(context) for name, value in self.options.items()}
        )
        with context.push({_DROPS: region["counts"]}):
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


def drop_tier(context: Any, rank: int) -> str:
    """The widest box a column of this rank leaves in, by its region's counts."""

    counts = context.get(_DROPS) or {}
    if rank and not any(counts.values()):
        raise ValueError(f"Drop rank {rank} is in a region that drops nothing: give the region its counts.")
    return next((tier for tier in TIERS if rank and rank <= counts.get(tier, 0)), "")


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
    region says how many go in each box. ``sort`` names the field a
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
        "drop_tier": drop_tier(context, drop),
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
