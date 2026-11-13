"""How a table region reads its own tables: each cell by its row and its
place in the row, and what the stylesheet is to read off it, which
``table_tags.mark_cells`` writes into the markup."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# A cell that says nothing: dashes and space, and nothing drawn but the empty
# value's own mark.
SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")


_SECTIONS = ("thead", "tbody", "tfoot")

# What a cell holds that is not on its row's line: words for assistive
# technology alone, and what the row keeps for its open line or shows only in
# a narrow box.
_OFF_LINE = frozenset({"visually-hidden", "row-below", "row-more", "narrow-only", "row-editor"})
# The kinds whose cells keep their line: a status's and a figure's.
_KEPT_KINDS = frozenset({"status", "numeric"})
# The kinds a cell is marked with as its heading is: a figure aligns and a
# control sizes to what it holds, which the stylesheet cannot read down a column.
_CELL_KINDS = frozenset({"numeric", "control"})
# The characters a line of prose or a status keeps whole; one longer wraps,
# and its cell holds out for a floor (`data-long`, tables.css).
LONG = 48

# The tags that end a line of a cell.
_LINE_ENDS = frozenset({"br", "div", "p", "li", "dt", "dd"})


# The role each class declares of what it marks in a cell, which the region
# writes as ``data-cell`` (``mark_cells``): free text that breaks wherever it
# must, and what a row means told in words.
ROLE_CLASSES = {"cell-data": "data", "cell-prose": "prose"}

# The tags of a control: a cell holding one and no heading is named "Actions".
_CONTROLS = frozenset({"a", "button", "select", "input"})
# What a row holds back for its open line: a part of a column that stays in a
# narrow box, a block kept beneath the row at any width, and the row's editor
# (`{% row_editor %}`), which is moved there and opens the row.
_PARTS = frozenset({"narrow-more", "row-below", "row-editor"})
_PART_KINDS = {"row-below": "below", "row-editor": "editor"}


Position = tuple[int, int]
Span = tuple[Position, Position]
# A place in the markup as the parser saw it, and how many characters past it:
# an attribute inside a start tag. One with no count is past the end tag there.
Point = tuple[Position, int | None]

# What a heading that sorts on the page adds to its cell, and the class of the
# mark its control ends with (`_table_heading.html`), which a table with fewer
# than two rows to sort has taken off.
_SORT_READY = ' aria-sort="none" data-sort-ready'
_SORT_MARK = "sort-indicator"
_CLASS_VALUE = re.compile(r"""\sclass\s*=\s*(["'])""")


@dataclass(slots=True)
class _Part:
    """An element a cell holds back for the row's open line: its class, its
    label, and its span from its start tag to its end tag."""

    kind: str
    label: str | None
    start: Position
    end: Position | None = None


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
    # Where the row's toggle starts, last in the cell that names the row: the
    # cell's kept line ends there, so the toggle stays the cell's own child.
    toggle: tuple[int, int] | None = None
    # Where each element that names the row starts: a leading `<strong>`, or
    # the `<strong>` in a leading link.
    names: list[tuple[int, int]] = field(default_factory=list)
    # Where the value of its start tag's class attribute starts, past its
    # start, or None when it has none.
    class_at: int | None = None
    # Whether it holds a row's toggle, and one that opens a folded group,
    # which is the row's own.
    toggled: bool = False
    folds: bool = False
    # A heading's sort control, as the spans that take it off again.
    sort: list[tuple[Point, Point]] = field(default_factory=list)
    sort_mark: Position | None = None


@dataclass(slots=True)
class _Row:
    section: str
    group: bool
    # Where its start tag is, and where the value of its class attribute
    # starts, past its start, or None when it has none.
    start: Position = (0, 0)
    class_at: int | None = None
    cells: list[_Cell] = field(default_factory=list)
    # Where its end tag is, after which its open line goes.
    end: tuple[int, int] | None = None


@dataclass(slots=True)
class LineEntry:
    """One part of a row's open line: its column's name, the markup it shows
    (whole elements, or a cell's content with the blocks cut out of it), and
    how it is shown: a ``column`` that left, a ``narrow`` part, a labeled
    block."""

    name: str
    spans: list[Span]
    how: str
    cut: list[Span] = field(default_factory=list)
    column: int = -1
    # The cell's classes its part keeps on the open line (``_CARRIED``), and
    # its role there: free text stays free text.
    css: tuple[str, ...] = ()
    data: bool = False
    # Named for a screen reader alone: a column with no heading.
    unseen: bool = False

    @property
    def whole(self) -> bool:
        return self.how != "column"


class Cells(HTMLParser):
    """Each cell of a region's own tables, by its row and its place in the row,
    and what the stylesheet is to read off it. Only the region's own tables: a
    table inside a cell is that cell's content, and says something."""

    def __init__(self, *, sorts: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        # Whether its tables' rows may sort on the page at all.
        self.sorts = sorts
        self.tables = 0
        # How many rows each of the region's own tables holds outside its head.
        self.bodies: list[int] = []
        self.section = ""
        self.rows: list[_Row] = []
        self.cell: _Cell | None = None
        # The tag of the cell itself, then of each element open inside it;
        # whether each was its parent's first element, and how many elements
        # each holds so far.
        self.open: list[str] = []
        self.firsts: list[bool] = []
        self.children: list[int] = []
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
        # Where each element or cell with a role starts, and its roles.
        self.roles: dict[tuple[int, int], set[str]] = {}
        # The content of each cell that keeps its line, from where it starts
        # to its cell's end tag.
        self.held: list[tuple[tuple[int, int], tuple[int, int]]] = []
        # Each row's open line: where it goes, how many columns it spans, the
        # blocks it shows whole and the parts it names.
        self.details: list[tuple[Position, int, list[Span], list[LineEntry]]] = []
        # What is taken out: the sort control of a table with too few rows.
        self.cuts: list[tuple[Point, Point]] = []
        # Where a gutter cell's class gets `row-gutter`, and where its row's
        # toggle goes.
        self.gutters: list[tuple[Point, str]] = []
        # Where each row's toggle goes, and whether its row is drawn open.
        self.toggles: list[tuple[Position, bool]] = []
        # Where an open row's class gets `is-expanded`: a row whose editor is open.
        self.opened: list[tuple[Point, str]] = []
        # Whether the sort control being read has its mark open.
        self.sorting: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        own = self.tables == 1
        if self.cell is not None and own and tag in ("td", "th", "tr", *_SECTIONS):
            self._close()
        if self.cell is not None:
            self._inside(self.cell, tag, attrs)
            if tag == "table":
                self.tables += 1
            return
        if tag == "table":
            self.tables += 1
            if self.tables == 1:
                self.rows, self.section = [], ""
                self.bodies.append(0)
        elif own and tag in _SECTIONS:
            self.section = tag
        elif own and tag == "tr":
            if self.section != "thead":
                self.bodies[-1] += 1
            found = _CLASS_VALUE.search(self.get_starttag_text() or "")
            self.rows.append(
                _Row(self.section, "row-group" in _classes(dict(attrs)), self.getpos(), found and found.end())
            )
        elif own and tag in ("td", "th") and self.rows:
            self._start_cell(dict(attrs))

    def _start_cell(self, attrs: dict[str, str | None]) -> None:
        line, column = self.getpos()
        tag_text = self.get_starttag_text() or ""
        # The start tag can run over lines: its content starts where its text ends.
        *above, last = tag_text.split("\n")
        opened = (line + len(above), (0 if above else column) + len(last))
        cell = self.cell = _Cell((line, column), attrs, opened)
        if found := _CLASS_VALUE.search(tag_text):
            cell.class_at = found.end()
        if self.section == "thead" and (ready := tag_text.find(_SORT_READY)) >= 0:
            cell.sort.append(((cell.start, ready), (cell.start, ready + len(_SORT_READY))))
        self.rows[-1].cells.append(cell)
        # Its tag is no end tag's, so the cell stays the floor of ``open``.
        self.open, self.firsts, self.children = [], [], []
        self.unsaid, self.off_line, self.part = None, None, None
        self._role(cell.start, cell.attrs)
        self._open(cell, "", _classes(cell.attrs), {})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cell is not None:
            self._inside(self.cell, tag, attrs, void=True)

    def _inside(self, cell: _Cell, tag: str, attrs: list[tuple[str, str | None]], *, void: bool = False) -> None:
        given = dict(attrs)
        classes = _classes(given)
        if tag in _LINE_ENDS:
            cell.line = 0
        if tag in _CONTROLS:
            cell.controls = True
        if "data-row-toggle" in given and cell.toggle is None and len(self.open) == 1:
            cell.toggle = self.getpos()
        if "data-row-toggle" in given:
            cell.toggled = True
            cell.folds = cell.folds or "data-fold-toggle" in given
        if self.section == "thead" and "data-sort-local" in given:
            at = self.getpos()
            cell.sort.append(((at, 0), (at, len(self.get_starttag_text() or ""))))
            self.sorting = True
        if self.sorting and _SORT_MARK in classes and cell.sort_mark is None:
            cell.sort_mark = self.getpos()
        first = not self.children[-1]
        self.children[-1] += 1
        # The row's name is what a cell leads with in bold, itself or as its
        # link's words: the row header of a table that has none.
        leads = len(self.open) == 1 or (len(self.open) == 2 and self.open[1] == "a" and self.firsts[1])
        if tag == "strong" and leads and (first or len(self.open) == 2) and "data-cell" not in given:
            cell.names.append(self.getpos())
        self._role(self.getpos(), given)
        # The line the region wraps a kept cell in is its own mark, read again
        # when the page is: what the cell holds is inside it.
        if tag != "br" and self.unsaid is None and "kept-line" not in classes and "empty-value" not in classes:
            cell.says_nothing = False
        if not void and tag not in _VOID:
            self._open(cell, tag, classes, given)

    def _open(self, cell: _Cell, tag: str, classes: list[str], attrs: dict[str, str | None]) -> None:
        depth = len(self.open)
        self.firsts.append(bool(self.children) and self.children[-1] == 1)
        self.children.append(0)
        self.open.append(tag)
        if self.unsaid is None and "empty-value" in classes:
            self.unsaid = depth
        if self.off_line is None and not _OFF_LINE.isdisjoint(classes):
            self.off_line = depth
        if self.part is None and depth and not _PARTS.isdisjoint(classes):
            self.part = depth
            kind = next((_PART_KINDS[name] for name in classes if name in _PART_KINDS), "narrow")
            cell.parts.append(_Part(kind, attrs.get("data-label"), self.getpos()))

    def _shut(self, cell: _Cell, tag: str) -> None:
        """Close the innermost open ``tag`` in the cell and everything opened inside it."""

        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth] == tag:
                del self.open[depth:], self.firsts[depth:], self.children[depth:]
                if self.unsaid is not None and self.unsaid >= depth:
                    self.unsaid = None
                if self.off_line is not None and self.off_line >= depth:
                    self.off_line = None
                if self.part is not None and self.part >= depth:
                    cell.parts[-1].end = self.getpos()
                    self.part = None
                return

    def handle_endtag(self, tag: str) -> None:
        if self.cell is not None and self.sorting and tag == "button":
            self.sorting = False
            if self.cell.sort_mark is not None:
                self.cell.sort.append(((self.cell.sort_mark, 0), (self.getpos(), None)))
        if self.cell is not None:
            self._shut(self.cell, tag)
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
        if not SAYS_NOTHING.fullmatch(data):
            self.cell.says_nothing = False

    def _role(self, start: Position, attrs: dict[str, str | None]) -> None:
        """Note the roles an element's classes declare. An element already
        marked was marked by a region inside this one."""

        roles = {ROLE_CLASSES[name] for name in _classes(attrs) if name in ROLE_CLASSES}
        if roles and "data-cell" not in attrs:
            self.roles.setdefault(start, set()).update(roles)

    def _close(self) -> None:
        self.cell = None
        self.sorting = False
        self.open, self.firsts, self.children = [], [], []
        self.unsaid = self.off_line = self.part = None

    def _mark_table(self) -> None:
        """What each cell says of itself that its heading cannot: whether it is
        blank and whether its whole column is, the line it keeps or the floor
        it holds out for, and a group's span; and each row's open line."""

        heads = [row.cells for row in self.rows if row.section == "thead"]
        kinds = _kinds(heads)
        named = _kept_naming_column(heads)
        records = [row for row in self.rows if row.section == "tbody" and not row.group]
        empty = _empty_columns(heads, named, records)
        width = _width(heads, records)
        names, drops, gutter = _columns(heads[-1] if heads else [])
        if not (self.sorts and _sorts(records)):
            self.cuts += _sort_controls(heads)
        for row in self.rows:
            if row.group:
                self._span(row, width)
                continue
            self._mark_cells(row, kinds, named, empty)
            self._name(row)
            if gutter is not None and row.section != "thead":
                self._gutter(row, gutter)
            if row.section == "tbody" and row.end:
                self._detail(row, width, names, drops - empty)

    def _open_row(self, row: _Row) -> None:
        """Mark the row open: its editor stands open on the line beneath it."""

        if row.class_at is None:
            self.opened.append(((row.start, None), ' class="is-expanded"'))
        else:
            self.opened.append(((row.start, row.class_at), "is-expanded "))

    def _gutter(self, row: _Row, place: int) -> None:
        """The cell of a row in the column whose heading keeps the gutter
        stands in the gutter, and a record's holds the toggle that opens it,
        last in the cell."""

        cell = _at_place(row.cells, place)
        if cell is None:
            return
        if "row-gutter" not in _classes(cell.attrs):
            if cell.class_at is None:
                self.gutters.append(((cell.start, None), ' class="row-gutter"'))
            else:
                self.gutters.append(((cell.start, cell.class_at), "row-gutter "))
        if row.section != "tbody" or cell.folds:
            return
        if cell.toggled:
            raise ValueError("A row's toggle in a column whose heading keeps the gutter is the region's to draw.")
        if cell.closed is None:
            raise ValueError("A cell in the gutter closes with its own end tag, before which its toggle goes.")
        self.toggles.append((cell.closed, any(_finished(each, "editor") for each in row.cells)))

    def _mark_cells(self, row: _Row, kinds: dict[int, str], named: int, empty: set[int]) -> None:
        # A column's kind is its heading's, which the stylesheet reads down the
        # column, so a cell is marked only with what the heading cannot say of it.
        body = row.section == "tbody"
        place = 0
        for index, cell in enumerate(row.cells):
            # A cell that says nothing is not counted as something its row holds back.
            marks = [" data-blank"] if body and cell.says_nothing else []
            # A figure's or a control's cell is marked as its heading is.
            if row.section != "thead" and _marked(cell, kind := kinds.get(place)):
                marks.append(f' data-kind="{kind}"')
            place += int(cell.attrs.get("colspan") or 1)
            if "colspan" not in cell.attrs:
                marks += self._column_marks(
                    cell, kinds.get(index), body=body, blank=index in empty, named=index == named
                )
            if marks:
                self.marks[cell.start] = marks

    def _column_marks(self, cell: _Cell, kind: str | None, *, body: bool, blank: bool, named: bool) -> list[str]:
        """What a cell that stands in one column says of itself there, and the
        line it keeps."""

        marks = [" data-blank-column"] if blank else []
        long = cell.longest > LONG
        # One `.kept-line`, which the stylesheet holds whole up to the
        # column's share of its region and lets wrap past that.
        if body and kind in _KEPT_KINDS and cell.closed and not cell.says_nothing and not long:
            self.held.append((cell.opened, cell.toggle or cell.closed))
            # Its line is the cell's lead now, so what it says names nothing.
            cell.names = []
        if body and not named and kind in ("prose", "status") and long:
            marks.append(" data-long")
        return marks

    def _name(self, row: _Row) -> None:
        """Mark what names the row, in each cell that leads with it."""

        for start in (start for cell in row.cells for start in cell.names):
            self.roles.setdefault(start, set()).add("name")

    def _detail(self, row: _Row, width: int, names: list[str], drops: set[int]) -> None:
        """The row's open line: the blocks it keeps beneath itself, then each
        part under its column's name: a column that may leave, with all its
        cell holds; a narrow part of a column that stays; a labeled block."""

        blocks: list[Span] = []
        entries: list[LineEntry] = []
        labeled: list[LineEntry] = []
        for index, cell in enumerate(row.cells):
            if "colspan" in cell.attrs or not cell.closed:
                continue
            # The row's editor leads its line, moved there: its ids are its own.
            editors = [span for _, span in _finished(cell, "editor")]
            self.cuts += [((start, 0), (end, None)) for start, end in editors]
            blocks[:0] = editors
            below = _finished(cell, "below")
            blocks += [span for label, span in below if not label]
            labeled += [LineEntry(label, [span], "label") for label, span in below if label]
            if entry := _column_entry(index, cell, (cell.opened, cell.closed), names, dropped=index in drops):
                entries.append(entry)
        if blocks or entries or labeled:
            self.details.append((row.end, width, blocks, entries + labeled))
        if any(_finished(cell, "editor") for cell in row.cells):
            self._open_row(row)

    def _span(self, row: _Row, width: int) -> None:
        """A group's heading row spans every column its table declares."""

        for cell in row.cells:
            if width and "colspan" not in cell.attrs:
                self.marks[cell.start] = [f' colspan="{width}"']


def _classes(attrs: dict[str, str | None]) -> list[str]:
    return (attrs.get("class") or "").split()


def _kept_naming_column(heads: list[list[_Cell]]) -> int:
    """The column that names each row, which never leaves."""

    named = _naming_column(heads)
    if rank := next((cell.attrs.get("data-drop") for cells in heads[-1:] for cell in cells[named : named + 1]), None):
        raise ValueError(f"The column that names each row has no drop rank, not {rank}.")
    return named


def _marked(cell: _Cell, kind: str | None) -> bool:
    return kind in _CELL_KINDS and "data-kind" not in cell.attrs and "colspan" not in cell.attrs


def _columns(heading: list[_Cell]) -> tuple[list[str], set[int], int | None]:
    """Each column's name, the columns that may leave, and the one whose
    heading keeps the gutter."""

    names = [" ".join("".join(cell.text).split()) for cell in heading]
    drops = {index for index, cell in enumerate(heading) if cell.attrs.get("data-drop")}
    gutters = (index for index, cell in enumerate(heading) if "row-gutter" in _classes(cell.attrs))
    return names, drops, next(gutters, None)


def _sorts(records: list[_Row]) -> bool:
    """Whether rows sort on the page: where there are two to put in order. A
    row that spans the table holds its place (tables.js, sortRows)."""

    return sum(not any("colspan" in cell.attrs for cell in row.cells) for row in records) >= 2


def _sort_controls(heads: list[list[_Cell]]) -> list[tuple[Point, Point]]:
    return [cut for cells in heads for cell in cells for cut in cell.sort]


def _at_place(cells: list[_Cell], place: int) -> _Cell | None:
    """The cell that starts at column ``place``, counting each cell's span;
    None when one spans across it or the row is shorter."""

    at = 0
    for cell in cells:
        if at == place:
            return None if "colspan" in cell.attrs else cell
        at += int(cell.attrs.get("colspan") or 1)
    return None


def _finished(cell: _Cell, kind: str) -> list[tuple[str | None, Span]]:
    """Each part of ``kind`` the cell holds back whose end tag was read, by
    its label and its span."""

    return [(part.label, (part.start, part.end)) for part in cell.parts if part.end and part.kind == kind]


def _column_name(names: list[str], index: int, cell: _Cell) -> tuple[str, bool]:
    """The name a cell's part goes under on the open line, and whether it is
    for a screen reader alone: a column of controls under no heading is still
    named."""

    name = names[index] if index < len(names) else ""
    if not name and cell.controls:
        return "Actions", True
    return name, False


# What a cell is that its part stays when the column leaves for the open line:
# a value that reads as a sentence, whose links are underlined among its words
# (base.css).
_CARRIED = ("prose",)


def _column_entry(index: int, cell: _Cell, content: Span, names: list[str], *, dropped: bool) -> LineEntry | None:
    """What a cell shows under its column's name on the open line: all a
    column that may leave holds, its blocks cut out, or the narrow parts of a
    column that stays; nothing when there is nothing of either."""

    name, unseen = _column_name(names, index, cell)
    if dropped:
        if cell.says_nothing:
            return None
        cut = [span for kind in ("below", "editor") for _, span in _finished(cell, kind)]
        classes = _classes(cell.attrs)
        carried = tuple(kind for kind in _CARRIED if kind in classes)
        return LineEntry(name, [content], "column", cut, index, carried, "cell-data" in classes, unseen)
    narrow = [span for _, span in _finished(cell, "narrow")]
    return LineEntry(name, narrow, "narrow", unseen=unseen) if narrow else None


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
