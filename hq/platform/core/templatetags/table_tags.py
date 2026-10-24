"""The tags a data table is declared through.

``{% table_region %}`` wraps the table in its labeled region,
``{% table_heading %}`` draws each column heading with its kind, its drop
rank and its sort control, and ``{% row_toggle %}`` draws the disclosure of a
row that holds more than it shows. A column's kind is the heading's, which the
stylesheet reads down the column; the region marks each cell only with what
the heading cannot say of it. Which columns show is the browser's to say, the
only thing that knows how wide a table is: tables.js lets a ranked column go
while its table is wider than its region, before the frame is painted, and
the row's toggle shows what left.
"""

import html
import inspect
import re
from dataclasses import dataclass, field
from functools import wraps
from html.parser import HTMLParser
from typing import Any

from django import template
from django.template.base import token_kwargs
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from hq.platform.application.tables import check_drop, column_kind

register = template.Library()

# A cell that says nothing: dashes and space, and nothing drawn but the empty
# value's own mark.
_SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")


_SECTIONS = ("thead", "tbody", "tfoot")

# What a cell holds that is not on its row's line: words for assistive
# technology alone, and what the row keeps for its open line or shows only in
# a narrow box.
_OFF_LINE = frozenset({"visually-hidden", "row-below", "row-more", "narrow-only"})
# The kinds whose cells keep their line: a status's and a figure's.
_KEPT_KINDS = frozenset({"status", "numeric"})
# The characters a line of prose or a status keeps whole; one longer wraps,
# and its cell holds out for a floor (`data-long`, tables.css).
LONG = 48

_TAG_NAME = re.compile(r"<\w+")

# The tags that end a line of a cell.
_LINE_ENDS = frozenset({"br", "div", "p", "li", "dt", "dd"})


# The tags of a control: a cell holding one and no heading is named "Actions".
_CONTROLS = frozenset({"a", "button", "select", "input"})
# What a row holds back for its open line: a part of a column that stays in a
# narrow box, and a block kept beneath the row at any width.
_PARTS = frozenset({"narrow-more", "row-below"})


_Position = tuple[int, int]
_Span = tuple[_Position, _Position]


@dataclass(slots=True)
class _Part:
    """An element a cell holds back for the row's open line: its class, its
    label, and its span from its start tag to its end tag."""

    kind: str
    label: str | None
    start: _Position
    end: _Position | None = None


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
    # Its words, kept for a heading, which names the column's part of the
    # open line.
    text: list[str] = field(default_factory=list)
    controls: bool = False
    parts: list[_Part] = field(default_factory=list)


@dataclass(slots=True)
class _Row:
    section: str
    group: bool
    cells: list[_Cell] = field(default_factory=list)
    # Where its end tag is, after which its open line goes.
    end: tuple[int, int] | None = None


@dataclass(slots=True)
class _Entry:
    """One part of a row's open line: its column's name, the markup it shows
    (whole elements, or a cell's content with the blocks cut out of it), and
    how it is shown: a ``column`` that left, a ``narrow`` part, a labelled
    block."""

    name: str
    spans: list[_Span]
    how: str
    cut: list[_Span] = field(default_factory=list)
    column: int = -1
    data: bool = False
    # Named for a screen reader alone: a column with no heading.
    unseen: bool = False

    @property
    def whole(self) -> bool:
        return self.how != "column"


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
        # The tag of the cell itself, then of each element open inside it.
        self.open: list[str] = []
        # The depth in ``open`` of the outermost element inside the empty
        # value's mark, of the outermost off the row's line, and of the
        # outermost part held back for the open line; None while none is
        # open. Decided where an element opens, so a piece of text reads two
        # fields and never walks back up the open elements.
        self.unsaid: int | None = None
        self.off_line: int | None = None
        self.part: int | None = None
        # Where each cell starts, with what is written into it there.
        self.marks: dict[tuple[int, int], list[str]] = {}
        # The content of each cell that keeps its line, from where it starts
        # to its cell's end tag.
        self.held: list[tuple[tuple[int, int], tuple[int, int]]] = []
        # Each row's open line: where it goes, how many columns it spans, the
        # blocks it shows whole and the parts it names.
        self.details: list[tuple[_Position, int, list[_Span], list[_Entry]]] = []

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
            # Its tag is no end tag's, so the cell stays the floor of ``open``.
            self.open, self.unsaid, self.off_line, self.part = [], None, None, None
            self._open("", _classes(self.cell.attrs), {})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cell is not None:
            self._inside(tag, attrs, void=True)

    def _inside(self, tag: str, attrs: list[tuple[str, str | None]], *, void: bool = False) -> None:
        assert self.cell is not None
        given = dict(attrs)
        classes = _classes(given)
        if tag in _LINE_ENDS:
            self.cell.line = 0
        if tag in _CONTROLS:
            self.cell.controls = True
        # The line the region wraps a kept cell in is its own mark, read again
        # when the page is: what the cell holds is inside it.
        if tag != "br" and self.unsaid is None and "kept-line" not in classes and "empty-value" not in classes:
            self.cell.says_nothing = False
        if not void and tag not in _VOID:
            self._open(tag, classes, given)

    def _open(self, tag: str, classes: list[str], attrs: dict[str, str | None]) -> None:
        depth = len(self.open)
        self.open.append(tag)
        if self.unsaid is None and "empty-value" in classes:
            self.unsaid = depth
        if self.off_line is None and not _OFF_LINE.isdisjoint(classes):
            self.off_line = depth
        if self.part is None and depth and not _PARTS.isdisjoint(classes):
            assert self.cell is not None
            self.part = depth
            kind = "below" if "row-below" in classes else "narrow"
            self.cell.parts.append(_Part(kind, attrs.get("data-label"), self.getpos()))

    def _shut(self, tag: str) -> None:
        """Close the innermost open ``tag`` in the cell and everything opened inside it."""

        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth] == tag:
                del self.open[depth:]
                if self.unsaid is not None and self.unsaid >= depth:
                    self.unsaid = None
                if self.off_line is not None and self.off_line >= depth:
                    self.off_line = None
                if self.part is not None and self.part >= depth:
                    assert self.cell is not None
                    self.cell.parts[-1].end = self.getpos()
                    self.part = None
                return

    def handle_endtag(self, tag: str) -> None:
        if self.cell is not None:
            self._shut(tag)
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
        if self.tables == 1 and tag == "tr" and self.rows:
            self.rows[-1].end = self.getpos()
        if self.tables == 1 and tag in _SECTIONS:
            self.section = ""

    def handle_data(self, data: str) -> None:
        if self.cell is None or self.unsaid is not None:
            return
        if self.section == "thead":
            self.cell.text.append(data)
        if self.off_line is None:
            self.cell.line += len(" ".join(data.split()))
            self.cell.longest = max(self.cell.longest, self.cell.line)
        if not _SAYS_NOTHING.fullmatch(data):
            self.cell.says_nothing = False

    def _close(self) -> None:
        self.cell = None
        self.open = []
        self.unsaid = self.off_line = self.part = None

    def _mark_table(self) -> None:
        """What each cell says of itself that its heading cannot: whether it is
        blank and whether its whole column is, the line it keeps or the floor
        it holds out for, and a group's span; and each row's open line."""

        heads = [row.cells for row in self.rows if row.section == "thead"]
        kinds = _kinds(heads)
        named = _naming_column(heads)
        if rank := next(
            (cell.attrs.get("data-drop") for cells in heads[-1:] for cell in cells[named : named + 1]), None
        ):
            raise ValueError(f"The column that names each row has no drop rank, not {rank}.")
        records = [row for row in self.rows if row.section == "tbody" and not row.group]
        empty = _empty_columns(heads, named, records)
        width = _width(heads, records)
        names = [" ".join("".join(cell.text).split()) for cell in heads[-1]] if heads else []
        drops = {index for index, cell in enumerate(heads[-1]) if cell.attrs.get("data-drop")} if heads else set()
        for row in self.rows:
            if row.group:
                self._span(row, width)
                continue
            self._mark_cells(row, kinds, named, empty)
            if row.section == "tbody" and row.end:
                self._detail(row, width, names, drops - empty)

    def _mark_cells(self, row: _Row, kinds: dict[int, str], named: int, empty: set[int]) -> None:
        body = row.section == "tbody"
        for index, cell in enumerate(row.cells):
            marks = [" data-blank"] if body and cell.says_nothing else []
            if "colspan" not in cell.attrs:
                marks += [" data-blank-column"] if index in empty else []
                long = cell.longest > LONG
                kept = kinds.get(index) in _KEPT_KINDS
                if body and kept and cell.closed and not cell.says_nothing and not long:
                    self.held.append((cell.opened, cell.closed))
                if body and index != named and kinds.get(index) in ("prose", "status") and long:
                    marks.append(" data-long")
            if marks:
                self.marks[cell.start] = marks

    def _detail(self, row: _Row, width: int, names: list[str], drops: set[int]) -> None:
        """The row's open line: the blocks it keeps beneath itself, then each
        part under its column's name: a column that may leave, with all its
        cell holds; a narrow part of a column that stays; a labelled block."""

        blocks: list[_Span] = []
        entries: list[_Entry] = []
        labelled: list[_Entry] = []
        for index, cell in enumerate(row.cells):
            if "colspan" in cell.attrs or not cell.closed:
                continue
            name = names[index] if index < len(names) else ""
            # A column of controls under no heading is still named, for a
            # screen reader alone.
            unseen = not name and cell.controls
            name = name or ("Actions" if unseen else "")
            below = [part for part in cell.parts if part.end and part.kind == "below"]
            narrow = [(part.start, part.end) for part in cell.parts if part.end and part.kind == "narrow"]
            for part in below:
                assert part.end
                if part.label:
                    labelled.append(_Entry(part.label, [(part.start, part.end)], "label"))
                else:
                    blocks.append((part.start, part.end))
            if index in drops:
                if not cell.says_nothing:
                    cut = [(part.start, part.end) for part in below if part.end]
                    data = "cell-data" in _classes(cell.attrs)
                    entries.append(_Entry(name, [(cell.opened, cell.closed)], "column", cut, index, data, unseen))
            elif narrow:
                entries.append(_Entry(name, narrow, "narrow", unseen=unseen))
        if blocks or entries or labelled:
            self.details.append((row.end, width, blocks, entries + labelled))

    def _span(self, row: _Row, width: int) -> None:
        """A group's heading row spans every column its table declares."""

        for cell in row.cells:
            if width and "colspan" not in cell.attrs:
                self.marks[cell.start] = [f' colspan="{width}"']


def _classes(attrs: dict[str, str | None]) -> list[str]:
    return (attrs.get("class") or "").split()


def _kinds(heads: list[list[_Cell]]) -> dict[int, str]:
    """Each column's kind, by place, from the heading that declares it: what a
    cell of a column that keeps its line (a status, a figure) says is wrapped
    whole, and a long cell of prose or a status holds out for a floor."""

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
_END_TAG = re.compile(r"</\s*\w+\s*>")
_TAGS = re.compile(r"<[^>]*>")


class _Markup:
    """The region's markup by the parser's positions, which count lines by
    newline alone."""

    def __init__(self, markup: str) -> None:
        self.markup = markup
        self.lines = [0] + [found.end() for found in re.finditer("\n", markup)]

    def at(self, position: _Position) -> int:
        row, column = position
        return self.lines[row - 1] + column

    def after(self, end_tag: _Position) -> int:
        """The offset past the end tag at ``end_tag``."""

        at = self.at(end_tag)
        found = _END_TAG.match(self.markup, at)
        return found.end() if found else at

    def content(self, span: _Span, cut: list[_Span] = [], *, whole: bool = False) -> str:  # noqa: B006 (never written)
        """The markup from ``span``'s start to its end tag, which a ``whole``
        element includes, with the elements in ``cut`` taken out."""

        parts, last = [], self.at(span[0])
        for start, end in sorted(cut, key=lambda each: self.at(each[0])):
            parts.append(self.markup[last : self.at(start)])
            last = self.after(end)
        parts.append(self.markup[last : self.after(span[1]) if whole else self.at(span[1])])
        return "".join(parts)


def _says_something(markup: str) -> bool:
    return not _SAYS_NOTHING.fullmatch(html.unescape(_TAGS.sub("", markup)))


def _detail_row(markup: _Markup, width: int, blocks: list[_Span], entries: list[_Entry]) -> str:
    """A row's open line, drawn after it and shown while it is open: a cell
    the table's width holding the blocks the row keeps beneath itself, then
    the parts it holds back, each named by its column. A column's part shows
    while the column has left (``data-dropped``, tables.js), a narrow part in
    a narrow box, a labelled block always."""

    drawn = [markup.content(block, whole=True) for block in blocks]
    listed = []
    for entry in entries:
        shown = "".join(markup.content(span, entry.cut, whole=entry.whole) for span in entry.spans)
        if entry.how == "column" and not _says_something(shown):
            continue
        how = f' data-column="{entry.column}"' if entry.how == "column" else f" data-{entry.how}"
        term = ' class="visually-hidden"' if entry.unseen else ""
        css = ' class="cell-data"' if entry.data else ""
        listed.append(f"<dt{how}{term}>{escape(entry.name)}</dt><dd{how}{css}>{shown}</dd>")
    if not drawn and not listed:
        return ""
    columns = f'<dl class="row-columns">{"".join(listed)}</dl>' if listed else ""
    return f'<tr class="row-detail" data-row-detail><td colspan="{width}">{"".join(drawn)}{columns}</td></tr>'


def mark_cells(markup: str) -> str:
    """The region's tables with each cell marked for what the stylesheet
    reads: a body cell that says nothing (``data-blank``), so it is not counted
    as something a row holds back; a cell of a column blank in every row
    (``data-blank-column``), which is not drawn; a cell of prose or a status
    longer than its floor (``data-long``); and a group's heading row spanning
    every column its headings declare (``colspan``). What a cell of a column
    that keeps its line says is one ``.kept-line``, so the stylesheet can hold
    the line whole up to the column's share of its region and let it wrap past
    that. After each row that holds something back is its open line
    (``_detail_row``), so opening a row moves nothing of the row itself."""

    cells = _Cells()
    cells.feed(markup)
    cells.close()
    if not cells.marks and not cells.held and not cells.details:
        return markup
    text = _Markup(markup)
    # The marks go straight after the tag's name: `<td` or `<th`.
    inserts = [(_TAG_NAME.match(markup, text.at(start)).end(), "".join(marks)) for start, marks in cells.marks.items()]
    for opened, closed in cells.held:
        inserts += [(text.at(opened), '<span class="kept-line">'), (text.at(closed), "</span>")]
    for end, width, blocks, entries in cells.details:
        inserts.append((text.after(end), _detail_row(text, width, blocks, entries)))
    parts, last = [], 0
    for offset, insert in sorted(inserts, key=lambda each: each[0]):
        parts += [markup[last:offset], insert]
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
) -> dict[str, Any]:
    """The labeled region a table sits in: the box it fits.
    An identifier in it is cut at a share of the region's width; ``room``
    names a smaller share for a row whose identifier sits beside wide columns
    (``shared``) or beside other identifiers (``snug``).

    A table fits its region, so the region is no scroll box and its heading
    pins under the page's chrome. Only a ``pane`` (a box of its own height, or
    a table sized to its content) and a ``chart``'s table scroll in it, and
    those are reachable from the keyboard."""

    if room not in _ROOMS:
        raise ValueError(f"A region's room is one of {sorted(_ROOMS)}, not {room!r}.")
    classes = [
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
            '<div class="table-scroll{}" role="region" aria-label="{}"{}{}>{}</div>',
            f" {region['css']}" if region["css"] else "",
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
    ``drop`` is the order a table too wide for its region lets the column go
    in, 1 first, the rightmost of a rank first. ``sort`` names the field a
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
