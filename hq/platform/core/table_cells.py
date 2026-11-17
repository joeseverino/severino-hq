"""How a table region reads its own tables: each cell by its row and its
place in the row, and what the stylesheet is to read off it, which
``table_tags.mark_cells`` writes into the markup."""

import re
from collections.abc import Sequence
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
# The characters of a line of a value past which it is longer than a line of
# words (`data-long`): it wraps, never to fewer than a line of words, and its
# column shares the room whatever its kind (tables.css); one of a kind that
# may be cut is held to a few lines (`cell-clamp`, cuts.css), and its row,
# opened, shows it whole.
LONG = 48
# The characters of the longest value of such a kind a column holds whole on
# its line, up to its share (`cell-short`, tables.css), counted generously: a
# proportional face sets most letters narrower than `ch`. An address and its
# port, a host, a mailbox or a project's name is no longer; a longer one wraps,
# never to fewer than a line of words, as a long one does.
SHORT = 32
# The kinds whose values wrap and may be clamped, whole where their row opens;
# the others (a status, a figure, a control) keep their line,
# and their column is as wide as it, unless one of its values is long.
_CUT_KINDS = frozenset({"name", "prose", "identifier"})
_HELD_KINDS = frozenset({"status", "numeric", "control"})
# What a cell holds that its row shows only on its open line: at any width
# (`row-more`, a list's rest), or once a narrow box lets it go (`narrow-more`).
_HELD = frozenset({"row-more", "line-rest", "narrow-more"})
# How many widths of its region a table's columns leave at, one rule each in
# tables.css.
RANKS = 6
# How many columns at most a table's share is counted over (`data-columns`),
# one rule each in tables.css.
COUNTED = 10

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
class _Part:
    """An element a cell moves to the row's open line: its kind, its label,
    and its span from its start tag to its end tag."""

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
    # Its words on the row's line: a heading's name its column's cells by.
    text: list[str] = field(default_factory=list)
    controls: bool = False
    parts: list[_Part] = field(default_factory=list)
    # What it holds that its row shows only on its open line (``_HELD``).
    holds: set[str] = field(default_factory=set)
    # Where each element that names the row starts: a leading `<strong>`, or
    # the `<strong>` in a leading link.
    names: list[tuple[int, int]] = field(default_factory=list)
    # Where the value of its start tag's class attribute starts, past its
    # start, or None when it has none.
    class_at: int | None = None
    # Whether each element at its top is a pill, and whether text stands
    # there beside them: a cell said by its pills alone is what a row is.
    tops: list[bool] = field(default_factory=list)
    loose: bool = False
    # Its first element at its top, and where a class goes on that element:
    # a value said by one list is its own short value's box.
    top_tag: str = ""
    top_class: Point | None = None
    # Whether an element in what it shows on its row's line carries a part's
    # role (`data-cell`), each of which a clamp holds on its own.
    parted: bool = False
    # Whether it holds a row's toggle, and one that opens a folded group,
    # which is the row's own.
    toggled: bool = False
    folds: bool = False
    # Where a toggle a template draws last in it starts: its value ends
    # there, so neither its clamp nor its copy on the open line holds it.
    toggle_at: tuple[int, int] | None = None

    @property
    def value_end(self) -> tuple[int, int] | None:
        return self.toggle_at or self.closed

    # A heading's sort control, as the spans that take it off again.
    sort: list[tuple[Point, Point]] = field(default_factory=list)
    sort_mark: Position | None = None
    # A heading's drop rank, as the span that takes it off again.
    drop: tuple[Point, Point] | None = None
    # Whether it holds a field a reader fills in, and a button.
    fields: bool = False
    presses: bool = False


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
    """A labeled block a row moves to its open line: its label and its span."""

    name: str
    span: Span


class Cells(HTMLParser):
    """Each cell of a region's own tables, by its row and its place in the row,
    and what the stylesheet is to read off it. Only the region's own tables: a
    table inside a cell is that cell's content, and says something."""

    def __init__(self, *, sorts: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        # Whether its tables' rows may sort on the page at all.
        self.sorts = sorts
        # Whether a row of the table being read can open
        # (`data-rows-can-open`), and whether a row of the region's tables
        # holds back a cell tables.js copies onto its open line.
        self.opens = False
        self.held = False
        # Whether the row being marked has its own toggle.
        self.toggled = False
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
        # one held whole up to its column's share (`cell-short`), and a row's
        # controls held to theirs (`cell-controls`).
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
        # element of the cell can be itself (``_box``).
        self.classed: dict[Point, list[str]] = {}
        # Where each row's toggle goes, and whether its row is drawn open.
        self.toggles: list[tuple[Position, bool]] = []
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
                self.table_at = self.getpos()
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
        if self.section == "thead" and (rank := _DROP.search(tag_text)):
            cell.drop = ((cell.start, rank.start()), (cell.start, rank.end()))
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
            cell.toggle_at = cell.toggle_at or self.getpos()
            cell.folds = cell.folds or "data-fold-toggle" in given
        if self.section == "thead" and "data-sort-local" in given:
            at = self.getpos()
            cell.sort.append(((at, 0), (at, len(self.get_starttag_text() or ""))))
            self.sorting = True
        if self.sorting and _SORT_MARK in classes and cell.sort_mark is None:
            cell.sort_mark = self.getpos()
        # A mark drawn on the line opens it as a word does.
        if tag == "svg" and self.off_line is None:
            cell.line += 1
        first = not self.children[-1]
        self.children[-1] += 1
        if len(self.open) == 1:
            cell.tops.append("pill" in classes)
            if len(cell.tops) == 1:
                found = _CLASS_VALUE.search(self.get_starttag_text() or "")
                cell.top_tag, cell.top_class = tag, (self.getpos(), found and found.end())
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
            cell.names.append(self.getpos())
        self._role(self.getpos(), given)
        if tag in _LINE_ENDS and self.off_line is None:
            cell.text.append(" ")
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
            cell.parts.append(_Part(kind, attrs.get("data-label"), self.getpos()))
        elif self.part is None and depth:
            cell.holds.update(_HELD.intersection(classes))

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
        if self.off_line is None:
            self.cell.text.append(data)
            self.cell.line += len(" ".join(data.split()))
            self.cell.longest = max(self.cell.longest, self.cell.line)
        if self.part is None and not SAYS_NOTHING.fullmatch(data):
            self.cell.says_nothing = False
        if len(self.open) == 1 and data.strip():
            self.cell.loose = True

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
        blank and whether its whole column is, its kind and the width its
        column leaves at, and where a long one is clamped; a group's span; and
        each row's open line."""

        heads = [row.cells for row in self.rows if row.section == "thead"]
        records = [row for row in self.rows if row.section == "tbody" and not row.group]
        kinds = _kinds(heads, records)
        # A table of row headers and no headings is a list of labels and
        # values: no column names its rows.
        named = -1 if not heads and kinds.get(0) == "status" else _kept_naming_column(heads)
        empty = _empty_columns(heads, named, records)
        longs = {
            place for row in records for place, cell in _placed(row) if "colspan" not in cell.attrs and _long(cell)
        }
        width = _width(heads, records)
        # How many columns stand while every one does, each column's share
        # (tables.css): its last row of headings, or with none its widest
        # row, less the columns with nothing in any row.
        counted = heads[-1] if heads else max((row.cells for row in records), key=len, default=[])
        if (columns := min(COUNTED, sum(index not in empty for index in range(len(counted))))) > 1:
            self.marks.setdefault(self.table_at, []).append(f' data-columns="{columns}"')
        drops, gutter = _columns(heads[-1] if heads else [])
        # The columns that never leave, whatever rank their heading gives
        # them: what a row is (a cell said by its pills alone; a pill beside a
        # value is a note on it), what it does (its controls, a field a reader
        # fills in), and words every row must show (`key-col`). A row
        # without them no longer says what it is or does what it is for.
        kept = {
            place
            for row in records
            for place, cell in _placed(row)
            if place != named and (cell.fields or (cell.tops and all(cell.tops) and not cell.loose))
        }
        kept |= {at for at, cell in enumerate(heads[-1] if heads else []) if "key-col" in _classes(cell.attrs)}
        kept |= {place for row in records for place, _ in _row_controls(row, kinds, named)}
        ranked = {at: rank for at, rank in drops.items() if at not in kept | empty}
        # Each rank is a width of the region (tables.css), counted from the
        # widest: a table's first rank leaves at the widest, and its others
        # each at the next width down. Nothing is measured, so a column may
        # leave a box that could still hold it.
        order = sorted(set(ranked.values()))
        if len(order) > RANKS:
            raise ValueError(f"A table's columns leave in at most {RANKS} ranks.")
        levels = {at: order.index(rank) + 1 for at, rank in ranked.items()}
        if heads and not drops:
            # A cell held back holds no button (`_detail`), so a column
            # holding one stands.
            pressed = {place for row in records for place, cell in _placed(row) if cell.presses}
            levels = _narrow_levels(heads[-1], kinds, kept | empty | pressed | {named})
        self.opens = False
        self.cuts += [cell.drop for row in self.rows if row.section == "thead" for cell in row.cells if cell.drop]
        if not (self.sorts and _sorts(records)):
            self.cuts += _sort_controls(heads)
        for row in self.rows:
            if row.group:
                self._span(row, width)
                continue
            # A value is clamped only where its row's toggle opens it whole.
            toggle_cell = None if gutter is None else _at_place(row.cells, gutter)
            self.toggled = toggle_cell is not None and not toggle_cell.folds
            self._mark_cells(row, kinds, named, empty, levels)
            if row.section == "thead":
                self._mark_long_heads(row, longs)
            self._name(row, kinds, named)
            if gutter is not None and row.section != "thead":
                self._gutter(row, gutter)
            elif row.section == "tbody" and not any(cell.toggled for cell in row.cells) and _moves(row):
                # What a row moves to its open line is reached by its toggle,
                # which its first cell holds where no heading keeps the gutter.
                self._gutter(row, 0)
            if row.section == "tbody" and row.end:
                self._detail(row, width, levels)
        if self.opens:
            self.marks.setdefault(self.table_at, []).append(" data-rows-can-open")

    def _open_row(self, row: _Row) -> None:
        """Mark the row open: its editor stands open on the line beneath it."""

        self._class((row.start, row.class_at), "is-expanded")

    def _gutter(self, row: _Row, place: int) -> None:
        """The cell of a row in the column whose heading keeps the gutter
        stands in the gutter, and a record's holds the toggle that opens it,
        last in the cell."""

        cell = _at_place(row.cells, place)
        if cell is None:
            return
        if "row-gutter" not in _classes(cell.attrs):
            self._class((cell.start, cell.class_at), "row-gutter")
        if row.section != "tbody" or cell.folds:
            return
        if cell.toggled:
            raise ValueError("A row's toggle in a column whose heading keeps the gutter is the region's to draw.")
        if cell.closed is None:
            raise ValueError("A cell in the gutter closes with its own end tag, before which its toggle goes.")
        self.toggles.append((cell.closed, any(_finished(each, "editor") for each in row.cells)))

    def _mark_cells(
        self,
        row: _Row,
        kinds: dict[int, str],
        named: int,
        empty: set[int],
        levels: dict[int, int],
    ) -> None:
        """What each cell says of itself: blank, its whole column blank, the
        width its column leaves at, and in the body its kind and its clamp."""

        body = row.section == "tbody"
        for at, cell in _placed(row):
            marks = [" data-blank"] if body and cell.says_nothing else []
            if "colspan" not in cell.attrs:
                marks += [" data-blank-column"] if at in empty else []
                marks += [f' data-drop="{levels[at]}"'] if at in levels else []
                if row.section != "thead":
                    marks += self._body_marks(cell, _kind_at(at, kinds, named), body=body)
            self.marks.setdefault(cell.start, []).extend(marks)

    def _mark_long_heads(self, row: _Row, longs: set[int]) -> None:
        """A heading over a column holding a long value says so: its column
        shares the room, whatever its kind."""

        for at, cell in _placed(row):
            if at in longs and "colspan" not in cell.attrs:
                self.marks.setdefault(cell.start, []).append(" data-long")

    def _body_marks(self, cell: _Cell, kind: str, *, body: bool) -> list[str]:
        """What a body cell says of itself under its column."""

        marks = [] if "data-kind" in cell.attrs else [f' data-kind="{kind}"']
        if body and _long(cell):
            marks.append(" data-long")
        words = kind in _CUT_KINDS or (kind == "control" and not cell.controls)
        if body and words and not cell.says_nothing:
            if _long(cell):
                if self.toggled and cell.closed:
                    self._box(cell, kind, "cell-clamp")
            # A fold's toggle is drawn before its head's value, which the
            # value's span (ended at a toggle) would leave out.
            elif cell.longest <= SHORT and cell.closed and not cell.folds:
                self._box(cell, kind, "cell-short")
        # A row's controls, more than one, keep their line up to their share
        # and past it wrap between them, so no row of buttons holds its table
        # wider than its region (tables.css).
        controls = kind == "control" and cell.controls and len(cell.tops) > 1
        if body and controls and not cell.fields and cell.closed:
            self._box(cell, kind, "cell-controls")
        return marks

    def _box(self, cell: _Cell, kind: str, mark: str) -> None:
        """Hold the cell's value in its clamp or its short value's box, the
        region's own box only where no element of the cell can be it. A clamp
        holds each part with a role on its own (cuts.css), so around parts
        the cell is the clamp; a value that is one list (a block of its own)
        is its own short value's box."""

        sole = len(cell.tops) == 1 and not cell.loose and cell.top_class is not None
        if mark == "cell-clamp" and (cell.parted or (cell.names and kind not in _HELD_KINDS)):
            self._class((cell.start, cell.class_at), mark)
        elif mark == "cell-short" and sole and cell.top_tag in _LISTS:
            self._class(cell.top_class, mark)
        else:
            self.boxes.setdefault(mark, []).append((cell.opened, cell.value_end))

    def _class(self, at: Point, name: str) -> None:
        """Add ``name`` to the class of the element whose start tag is at
        ``at``: past its class value's start (``at``'s count), or where it
        has none, a class attribute of its own."""

        self.classed.setdefault(at, []).append(name)

    def _name(self, row: _Row, kinds: dict[int, str], named: int) -> None:
        """Mark what names the row, in each cell of words that leads with it:
        a held column's bold (a status said in bold) names nothing."""

        for at, cell in _placed(row):
            if _kind_at(at, kinds, named) in _HELD_KINDS:
                continue
            for start in cell.names:
                self.roles.setdefault(start, set()).add("name")

    def _detail(self, row: _Row, width: int, levels: dict[int, int]) -> None:
        """The row's open line as the server draws it: the blocks it moves
        there (its editor first, then each block it keeps beneath itself and
        its buttons past the first few), each labeled one under its label.
        What a cell holds back (its column left, `data-drop`; a part only the
        open line shows, `row-more` or `line-rest`) is written once, in the
        cell: tables.js copies it onto the line as the row opens, and a
        narrow box shows the row's `narrow-more` parts in place.
        So a cell that holds something back holds no control, which a copy
        would draw twice."""

        blocks: list[Span] = []
        entries: list[LineEntry] = []
        always = False
        for at, cell in _placed(row):
            if "colspan" in cell.attrs or not cell.closed:
                continue
            # The row's editor leads its line, moved there: its ids are its own.
            editors = [span for _, span in _finished(cell, "editor")]
            blocks[:0] = editors
            moved = _finished(cell, "below")
            self.cuts += [((start, 0), (end, None)) for start, end in [*editors, *(span for _, span in moved)]]
            blocks += [span for label, span in moved if not label]
            entries += [LineEntry(label, span) for label, span in moved if label]
            shown = bool(cell.holds & {"row-more", "line-rest"}) and not cell.says_nothing
            if (shown or at in levels) and not cell.says_nothing:
                if cell.presses or cell.fields:
                    raise ValueError(
                        "A cell that holds something back for its row's open line holds no control: "
                        "it stands in place or moves there whole (row-below)."
                    )
                self.held = True
            always = always or bool(moved) or shown
            self.opens = self.opens or shown or at in levels or "narrow-more" in cell.holds
        if always:
            self.marks.setdefault(row.start, []).append(" data-below")
        if blocks or entries:
            self.details.append((row.end, width, blocks, entries))
        if any(_finished(cell, "editor") for cell in row.cells):
            self._open_row(row)

    def _span(self, row: _Row, width: int) -> None:
        """A group's heading row spans every column its table declares."""

        for cell in row.cells:
            marks = []
            if width and "colspan" not in cell.attrs:
                marks.append(f' colspan="{width}"')
            self.marks.setdefault(cell.start, []).extend(marks)


def _long(cell: _Cell) -> bool:
    """Whether a line of the cell runs longer than a line of words
    (``data-long``): what it says wraps, and its column shares the room."""

    return cell.longest > LONG and not cell.says_nothing


def _classes(attrs: dict[str, str | None]) -> list[str]:
    return (attrs.get("class") or "").split()


def _row_controls(row: _Row, kinds: dict[int, str], named: int) -> list[tuple[int, _Cell]]:
    """Each cell of the row's controls, by its place: a row's actions or a
    cell of a column of controls, holding a control or the words that stand
    where a row has none. Never the cell that names the row, a column of
    tick-boxes, nor a field a reader fills in, which is the row's value."""

    found = []
    for at, cell in _placed(row):
        classes = _classes(cell.attrs)
        if at == named or "colspan" in cell.attrs or "select-column" in classes or not cell.closed:
            continue
        controls = "control" in (kinds.get(at), cell.attrs.get("data-kind")) or "row-actions" in classes
        if controls and not cell.fields and (cell.controls or not cell.says_nothing):
            found.append((at, cell))
    return found


def _placed(row: _Row) -> list[tuple[int, _Cell]]:
    """Each cell of the row with the column it starts at, past the spans before it."""

    found, place = [], 0
    for cell in row.cells:
        found.append((place, cell))
        place += int(cell.attrs.get("colspan") or 1)
    return found


def _kept_naming_column(heads: list[list[_Cell]]) -> int:
    """The column that names each row, which never leaves."""

    named = _naming_column(heads)
    if rank := next((cell.attrs.get("data-drop") for cells in heads[-1:] for cell in cells[named : named + 1]), None):
        raise ValueError(f"The column that names each row has no drop rank, not {rank}.")
    return named


def leaving_order(columns: Sequence[tuple[str, Sequence[str]]]) -> list[int]:
    """The places of a table's columns, each by its kind and its classes, in
    the order a narrow table lets them go: ``optional-col`` columns first,
    then the rest, each from the far end. Never the first, which names the
    row, a ``key-col``, a column of the row's controls, or the closing figure
    of a table that marks no key."""

    keyed = any("key-col" in classes for _, classes in columns)
    last = len(columns) - 1

    def spare(index: int, kind: str, classes: Sequence[str]) -> bool:
        closing_figure = not keyed and index == last and kind == "numeric"
        return index > 0 and "key-col" not in classes and not closing_figure and kind != "control"

    far_first = [index for index in reversed(range(len(columns))) if spare(index, *columns[index])]
    return sorted(far_first, key=lambda index: "optional-col" not in columns[index][1])


def _narrow_levels(heading: list[_Cell], kinds: dict[int, str], kept: set[int]) -> dict[int, int]:
    """The levels of a table whose headings rank no column: each column a
    row can do without (`leaving_order`) leaves at the narrowest widths, the
    first to go at the widest of them and the last below the narrowest rank's
    width, so a table keeps every column until a narrow box and is never
    wider than its region there."""

    columns = [(kinds.get(at, ""), _classes(cell.attrs)) for at, cell in enumerate(heading)]
    spare = [at for at in leaving_order(columns) if at not in kept]
    return {at: max(1, RANKS - len(spare) + place) for place, at in enumerate(spare, 1)}


def _columns(heading: list[_Cell]) -> tuple[dict[int, int], int | None]:
    """The rank of each column that may leave, and the one whose heading
    keeps the gutter."""

    drops = {index: int(cell.attrs["data-drop"]) for index, cell in enumerate(heading) if cell.attrs.get("data-drop")}
    gutters = (index for index, cell in enumerate(heading) if "row-gutter" in _classes(cell.attrs))
    return drops, next(gutters, None)


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
    """Each part of ``kind`` the cell moves whose end tag was read, by its
    label and its span."""

    return [(part.label, (part.start, part.end)) for part in cell.parts if part.end and part.kind == kind]


def _moves(row: _Row) -> bool:
    """Whether the row moves a part to its open line (`row-below`)."""

    return any(_finished(cell, "below") for cell in row.cells)


def _kinds(heads: list[list[_Cell]], body: list[_Row]) -> dict[int, str]:
    """Each column's kind, by place, from the heading that declares it. A
    column of a row's actions is its controls whatever its heading says, so
    they keep one line. A table with no headings is read off its rows, where
    a row's header is held to what it says."""

    actions = {place for row in body for place, cell in _placed(row) if "row-actions" in _classes(cell.attrs)}
    if heads:
        declared = {
            index: kind for cells in heads for index, cell in enumerate(cells) if (kind := cell.attrs.get("data-kind"))
        }
        return declared | dict.fromkeys(actions, "control")
    kinds: dict[int, str] = {}
    for row in body:
        for index, cell in enumerate(row.cells):
            if "row-actions" in _classes(cell.attrs) or cell.attrs.get("data-kind") == "control":
                kinds[index] = "control"
            elif cell.attrs.get("scope") == "row":
                kinds.setdefault(index, "status")
    return kinds


def _kind_at(at: int, kinds: dict[int, str], named: int) -> str:
    """The kind of the column at ``at``: the naming column's own, else its heading's."""

    return "name" if at == named else kinds.get(at, "prose")


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


# The elements that are a block of their own, so a value said by one alone is
# its own short value's box (tables.css).
_LISTS = frozenset({"ul", "ol"})

_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
