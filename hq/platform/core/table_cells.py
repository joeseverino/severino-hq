"""How a table region reads a table written by hand: each cell by its row and
its place in the row, as the facts ``table_layout.lay_out`` decides its marks
from, and where in the markup each mark goes, which ``table_tags.mark_cells``
writes."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .table_layout import HELD, CellFacts, Moved, RowFacts, lay_out

# A cell that says nothing: dashes and space, and nothing drawn but the empty
# value's own mark.
SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")


_SECTIONS = ("thead", "tbody", "tfoot")

# What a cell holds that is not on its row's line: words for assistive
# technology alone, and what the row keeps for its open line or shows only in
# a narrow box.
_OFF_LINE = frozenset({"visually-hidden", "row-below", "row-more", "narrow-only", "row-editor"})

# The tags that end a line of a cell.
_LINE_ENDS = frozenset({"br", "div", "p", "li", "dt", "dd"})


# The role each class declares of what it marks in a cell, which the region
# writes as ``data-cell`` (``mark_cells``): free text that breaks wherever it
# must, and what a row means told in words.
ROLE_CLASSES = {"cell-data": "data", "cell-prose": "prose"}

# The tags of a control, which a row's controls hold.
_CONTROLS = frozenset({"a", "button", "select", "input"})
# The input types that are no field a reader fills in: a cell holding a field
# holds a value of the row (a choice of a rule), not what the row offers.
_UNFILLED = frozenset({"hidden", "submit", "button"})
# What a row moves to its open line: a block kept beneath the row, its
# buttons past the first few, and the row's editor (`{% row_editor %}`),
# which opens the row.
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
_DROP = re.compile(r"""\sdata-drop\s*=\s*(["'])[^"']*\1""")


@dataclass(slots=True)
class _Cell(CellFacts):
    """A cell's facts, and where in the markup each of its marks goes."""

    start: Position = (0, 0)
    attrs: dict[str, str | None] = field(default_factory=dict)
    # Where its content starts (past the start tag) and, when the cell closes
    # with its own end tag, where that tag is.
    opened: Position = (0, 0)
    closed: Position | None = None
    # The characters of text on its line so far.
    line: int = 0
    # Where each element that names the row starts: a leading `<strong>`, or
    # the `<strong>` in a leading link.
    name_starts: list[Position] = field(default_factory=list)
    # Where the value of its start tag's class attribute starts, past its
    # start, or None when it has none, and where a class goes on its top element.
    class_at: int | None = None
    top_class: Point | None = None
    # Where a toggle a template draws last in it starts: its value ends
    # there, so neither its clamp nor its copy on the open line holds it.
    toggle_at: Position | None = None
    # A heading's sort control, as the spans that take it off again.
    sort: list[tuple[Point, Point]] = field(default_factory=list)
    sort_mark: Position | None = None
    # A heading's drop rank, as the span that takes it off again.
    drop_span: tuple[Point, Point] | None = None

    @property
    def value_end(self) -> Position | None:
        return self.toggle_at or self.closed


@dataclass(slots=True)
class _Row(RowFacts):
    # Where its start tag is, and where the value of its class attribute
    # starts, past its start, or None when it has none.
    start: Position = (0, 0)
    class_at: int | None = None
    # Where its end tag is, after which its open line goes.
    end: Position | None = None


@dataclass(slots=True)
class LineEntry:
    """A labeled block a row moves to its open line: its label and its span."""

    name: str
    span: Span


class Cells(HTMLParser):
    """Each cell of a region's own tables, by its row and its place in the row,
    and where each mark ``lay_out`` gives it goes. Only the region's own
    tables: a table inside a cell is that cell's content, and says something."""

    def __init__(self, *, sorts: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        # Whether its tables' rows may sort on the page at all.
        self.sorts = sorts
        # Whether a row of the region's tables holds back a cell tables.js
        # copies onto its open line.
        self.held = False
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
        # outermost part moved to the open line; None while none is open.
        # Decided where an element opens, so a piece of text reads two fields
        # and never walks back up the open elements.
        self.unsaid: int | None = None
        self.off_line: int | None = None
        self.part: int | None = None
        # Where each table, row or cell starts, with what is written into it there.
        self.marks: dict[tuple[int, int], list[str]] = {}
        # Where the table being read starts.
        self.table_at: Position = (0, 0)
        # Where each element or cell with a role starts, and its roles.
        self.roles: dict[tuple[int, int], set[str]] = {}
        # The content of each cell a box of the region's own holds, by the
        # box's class: a long one held to a few lines (`cell-clamp`), a short
        # one held whole up to its column's share (`cell-short`).
        self.boxes: dict[str, list[Span]] = {}
        # Each row's open line: where it goes, how many columns it spans, the
        # blocks it shows whole and the entries it names.
        self.details: list[tuple[Position, int, list[Span], list[LineEntry]]] = []
        # What is taken out: the sort control of a table with too few rows, a
        # heading's rank, and what a row moves to its open line.
        self.cuts: list[tuple[Point, Point]] = []
        # Where a class is added to an element's own: a gutter cell's
        # `row-gutter`, an open row's `is-expanded` (a row whose editor is
        # open), and the mark of a clamp or a short value's box that an
        # element of the cell can be itself.
        self.classed: dict[Point, list[str]] = {}
        # Where each row's toggle goes, and whether its row is drawn open.
        self.toggles: list[tuple[Position, bool]] = []
        # Whether the sort control being read has its mark open.
        self.sorting: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "dialog":
            # A dialog is a layer of its own, never part of a row or its line,
            # so the parser passes over it whole to its end tag.
            self.set_cdata_mode(tag)
            return
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
                self.table_at = self.getpos()
                self.bodies.append(0)
        elif own and tag in _SECTIONS:
            self.section = tag
        elif own and tag == "tr":
            if self.section != "thead":
                self.bodies[-1] += 1
            found = _CLASS_VALUE.search(self.get_starttag_text() or "")
            group = "row-group" in _classes(dict(attrs))
            self.rows.append(_Row(self.section, group, ends=False, start=self.getpos(), class_at=found and found.end()))
        elif own and tag in ("td", "th") and self.rows:
            self._start_cell(dict(attrs))

    def _start_cell(self, attrs: dict[str, str | None]) -> None:
        line, column = self.getpos()
        tag_text = self.get_starttag_text() or ""
        # The start tag can run over lines: its content starts where its text ends.
        *above, last = tag_text.split("\n")
        opened = (line + len(above), (0 if above else column) + len(last))
        classes = _classes(attrs)
        cell = self.cell = _Cell(
            colspan=int(attrs.get("colspan") or 1),
            spans="colspan" in attrs,
            classes=tuple(classes),
            kind=attrs.get("data-kind") or "",
            drop=int(attrs.get("data-drop") or 0),
            row_header=attrs.get("scope") == "row",
            closes=False,
            start=(line, column),
            attrs=attrs,
            opened=opened,
        )
        if found := _CLASS_VALUE.search(tag_text):
            cell.class_at = found.end()
        if self.section == "thead" and (ready := tag_text.find(_SORT_READY)) >= 0:
            cell.sort.append(((cell.start, ready), (cell.start, ready + len(_SORT_READY))))
        if self.section == "thead" and (rank := _DROP.search(tag_text)):
            cell.drop_span = ((cell.start, rank.start()), (cell.start, rank.end()))
        self.rows[-1].cells.append(cell)
        # Its tag is no end tag's, so the cell stays the floor of ``open``.
        self.open, self.firsts, self.children = [], [], []
        self.unsaid, self.off_line, self.part = None, None, None
        self._role(cell.start, cell.attrs, classes)
        self._open(cell, "", classes, {})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cell is not None:
            self._inside(self.cell, tag, attrs, void=True)

    def _inside(self, cell: _Cell, tag: str, attrs: list[tuple[str, str | None]], *, void: bool = False) -> None:
        given = dict(attrs)
        classes = _classes(given)
        at = self.getpos()
        if tag in _LINE_ENDS:
            cell.line = 0
        # A control refused here stands as a button that cannot be pressed.
        if tag in _CONTROLS or given.get("role") == "button":
            cell.controls = True
        if tag in ("select", "textarea") or (tag == "input" and given.get("type") not in _UNFILLED):
            cell.fields = True
        pressed = tag == "button" or given.get("role") == "button"
        if (
            pressed or (tag == "input" and given.get("type") in ("submit", "button"))
        ) and "data-row-toggle" not in given:
            cell.presses = True
        if "data-row-toggle" in given:
            cell.toggled = True
            cell.toggle_at = cell.toggle_at or at
            cell.folds = cell.folds or "data-fold-toggle" in given
        if self.section == "thead" and "data-sort-local" in given:
            cell.sort.append(((at, 0), (at, len(self.get_starttag_text() or ""))))
            self.sorting = True
        if self.sorting and _SORT_MARK in classes and cell.sort_mark is None:
            cell.sort_mark = at
        # A mark drawn on the line opens it as a word does.
        if tag == "svg" and self.off_line is None:
            cell.line += 1
        first = not self.children[-1]
        self.children[-1] += 1
        if len(self.open) == 1:
            cell.tops.append("pill" in classes)
            cell.said.append(False)
            if len(cell.tops) == 1:
                found = _CLASS_VALUE.search(self.get_starttag_text() or "")
                cell.top_tag, cell.top_class, cell.top_classed = tag, (at, found and found.end()), True
        if (
            self.off_line is None
            and self.part is None
            and ("data-cell" in given or not ROLE_CLASSES.keys().isdisjoint(classes))
        ):
            cell.parted = True
        # The row's name is what a cell leads with in bold, itself or as its
        # link's words: the row header of a table that has none.
        leads = len(self.open) == 1 or (len(self.open) == 2 and self.open[1] == "a" and self.firsts[1])
        if tag == "strong" and leads and (first or len(self.open) == 2) and "data-cell" not in given:
            cell.name_starts.append(at)
            cell.names += 1
        self._role(at, given, classes)
        # What moves whole to the row's open line says nothing on its line.
        moves = self.part is not None or not _PART_KINDS.keys().isdisjoint(classes)
        if tag != "br" and self.unsaid is None and not moves and "empty-value" not in classes:
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
        if self.part is None and depth and (kind := next((_PART_KINDS[c] for c in classes if c in _PART_KINDS), None)):
            self.part = depth
            cell.moved.append(Moved(kind, attrs.get("data-label"), (self.getpos(), None), finished=False))
        elif self.part is None and depth:
            cell.holds.update(HELD.intersection(classes))

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
                    part = cell.moved[-1]
                    part.content, part.finished = (part.content[0], self.getpos()), True
                    self.part = None
                return

    def handle_endtag(self, tag: str) -> None:
        if tag == "dialog":
            return
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
                self.cell.closed, self.cell.closes = self.getpos(), True
            self._close()
        if self.tables == 1 and tag == "tr" and self.rows:
            self.rows[-1].end, self.rows[-1].ends = self.getpos(), True
        if self.tables == 1 and tag in _SECTIONS:
            self.section = ""

    def handle_data(self, data: str) -> None:
        if self.cell is None or self.unsaid is not None or self.cdata_elem == "dialog":
            return
        if self.off_line is None:
            self.cell.line += len(" ".join(data.split()))
            self.cell.longest = max(self.cell.longest, self.cell.line)
        if self.part is None and not SAYS_NOTHING.fullmatch(data):
            self.cell.says_nothing = False
        if len(self.open) == 1 and data.strip():
            self.cell.loose = True
        elif self.cell.said and data.strip():
            self.cell.said[-1] = True

    def _role(self, start: Position, attrs: dict[str, str | None], classes: list[str]) -> None:
        """Note the roles an element's classes declare. An element already
        marked was marked by a region inside this one."""

        roles = {ROLE_CLASSES[name] for name in classes if name in ROLE_CLASSES}
        if roles and "data-cell" not in attrs:
            self.roles.setdefault(start, set()).update(roles)

    def _close(self) -> None:
        self.cell = None
        self.sorting = False
        self.open, self.firsts, self.children = [], [], []
        self.unsaid = self.off_line = self.part = None

    def _mark_table(self) -> None:
        """Lay the table out, and note where each mark it is given goes."""

        table = lay_out(self.rows, sorts=self.sorts)
        self.held = self.held or table.held
        if table.marks:
            self.marks.setdefault(self.table_at, []).extend(table.marks)
        self.cuts += [
            cell.drop_span for row in self.rows if row.section == "thead" for cell in row.cells if cell.drop_span
        ]
        if not table.sorts:
            self.cuts += [cut for row in self.rows if row.section == "thead" for cell in row.cells for cut in cell.sort]
        for row in self.rows:
            for cell in row.cells:
                self.marks.setdefault(cell.start, []).extend(cell.marks)
                for name in cell.added:
                    self.classed.setdefault((cell.start, cell.class_at), []).append(name)
                if cell.top_class is not None:
                    for name in cell.top_added:
                        self.classed.setdefault(cell.top_class, []).append(name)
                if cell.box:
                    self.boxes.setdefault(cell.box, []).append((cell.opened, cell.value_end))
                if cell.toggle is not None and cell.closed is not None:
                    self.toggles.append((cell.closed, cell.toggle))
                if cell.named:
                    for start in cell.name_starts:
                        self.roles.setdefault(start, set()).add("name")
            if row.marks:
                self.marks.setdefault(row.start, []).extend(row.marks)
            for name in row.added:
                self.classed.setdefault((row.start, row.class_at), []).append(name)
            if row.detail and row.end is not None:
                width, blocks, entries = row.detail
                self.cuts += [((part.content[0], 0), (part.content[1], None)) for part in (*blocks, *entries)]
                self.details.append(
                    (
                        row.end,
                        width,
                        [part.content for part in blocks],
                        [LineEntry(e.label, e.content) for e in entries],
                    )
                )


def _classes(attrs: dict[str, str | None]) -> list[str]:
    return (attrs.get("class") or "").split()


_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
