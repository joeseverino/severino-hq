"""How a table region reads its own tables: each cell by its row and its
place in the row, and what the stylesheet is to read off it, which
``table_tags.mark_cells`` writes into the markup."""

import html
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
# The characters of a line of a cut value that shows whole on one line of
# its column at its floor; a row holding a longer one offers its toggle, so
# its open line shows the value whole (`data-long`, tables.css).
LONG = 48
# The characters of a run that cannot break (a value standing alone, a chip)
# that fit a rem of its column's floor, and those its cell's padding takes.
_RUN_PER_REM = 2
_RUN_PADDING = 4

# Each kind's track in the table's grid (`--tracks`, tables.css), the one
# place a column's width is declared: its floor, the least it is drawn at
# with its cells' padding, and whether it shares what the region has past
# the held columns (`share`) or is as wide as what it holds (`hold`). The
# naming column is a kind of its own. The region lets a ranked column go
# while it is narrower than the floors of the columns it draws, and nothing
# is measured.
TRACKS = {
    "name": ("14rem", "share"),
    "prose": ("9rem", "share"),
    "identifier": ("9rem", "share"),
    "status": ("7rem", "hold"),
    "numeric": ("6rem", "hold"),
    "control": ("10rem", "hold"),
}
# Beside the row's controls the words that name it keep a line of
# `--row-words`, so they never read a few words a line beside a column of
# buttons; once the controls stand beneath them they need no more than the floor.
_BESIDE_CONTROLS = "calc(var(--row-words) + 2 * var(--space-6))"
# The padding of a cell drawn across its row (a column let go in an open row,
# controls beneath the words, a group's heading): the toggle's gutter and the
# cell's far edge (tables.css).
_SPANNING = "calc(var(--row-gutter) + var(--space-6))"
# The kinds whose cells are cut: a sentence at its third line, an identifier
# at the end of its line. Each says its whole value in its title.
_CUT_KINDS = frozenset({"name", "prose", "identifier"})

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
# What a row holds back for its open line: a block kept beneath the row at
# any width, and the row's editor (`{% row_editor %}`), which is moved there
# and opens the row.
_PARTS = frozenset({"row-below", "row-editor"})
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
    # The characters of the run that cannot break being read (a value standing
    # alone, a chip), and of the longest such run.
    held: int = 0
    run: int = 0
    # Its words on the row's line: a heading's name its column's cells by
    # (`data-label`), a cut cell's say its whole value (`title`).
    text: list[str] = field(default_factory=list)
    controls: bool = False
    parts: list[_Part] = field(default_factory=list)
    # Where each element that names the row starts: a leading `<strong>`, or
    # the `<strong>` in a leading link.
    names: list[tuple[int, int]] = field(default_factory=list)
    # Where the value of its start tag's class attribute starts, past its
    # start, or None when it has none.
    class_at: int | None = None
    # Whether each element at its top is a pill, and whether text stands
    # there beside them: a status said by one pill alone is as wide as its
    # column (`data-pill-only`), so the pills down a column line up.
    tops: list[bool] = field(default_factory=list)
    loose: bool = False
    # Whether it holds a row's toggle, and one that opens a folded group,
    # which is the row's own.
    toggled: bool = False
    folds: bool = False
    # A heading's sort control, as the spans that take it off again.
    sort: list[tuple[Point, Point]] = field(default_factory=list)
    sort_mark: Position | None = None
    # A heading's drop rank, as the span that takes it off a column that never leaves.
    drop: tuple[Point, Point] | None = None
    # Whether it holds a field a reader fills in.
    fields: bool = False


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
    """A labeled block of a row's open line: its label and the element it shows."""

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
        self.unbroken: int | None = None
        # Where each table, section, row or cell starts, with what is written
        # into it there.
        self.marks: dict[tuple[int, int], list[str]] = {}
        # Where the table being read starts, and each of its sections.
        self.table_at: Position = (0, 0)
        self.sections: list[Position] = []
        # Where each element or cell with a role starts, and its roles.
        self.roles: dict[tuple[int, int], set[str]] = {}
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
                self.rows, self.section, self.sections = [], "", []
                self.table_at = self.getpos()
                self.bodies.append(0)
        elif own and tag in _SECTIONS:
            self.section = tag
            self.sections.append(self.getpos())
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
        self.unsaid, self.off_line, self.part, self.unbroken = None, None, None, None
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
        if "data-row-toggle" in given:
            cell.toggled = True
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
        # A value standing alone after words on its line (a host after "on")
        # is text of that line: a box of its own leaves the word alone above it.
        if "data-sole" in given and "token" in classes and cell.line and self.off_line is None:
            self.marks.setdefault(self.getpos(), []).append(" data-in-line")
        first = not self.children[-1]
        self.children[-1] += 1
        if len(self.open) == 1:
            cell.tops.append("pill" in classes)
        # The row's name is what a cell leads with in bold, itself or as its
        # link's words: the row header of a table that has none.
        leads = len(self.open) == 1 or (len(self.open) == 2 and self.open[1] == "a" and self.firsts[1])
        if tag == "strong" and leads and (first or len(self.open) == 2) and "data-cell" not in given:
            cell.names.append(self.getpos())
        self._role(self.getpos(), given)
        if tag in _LINE_ENDS and self.off_line is None:
            cell.text.append(" ")
        if tag != "br" and self.unsaid is None and "empty-value" not in classes:
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
        if self.unbroken is None and depth and (tag == "code" or "data-sole" in attrs):
            self.unbroken, cell.held = depth, 0
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
                if self.unbroken is not None and self.unbroken >= depth:
                    self.unbroken = None
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
            if self.unbroken is not None:
                self.cell.held += len(data.strip())
                self.cell.run = max(self.cell.run, self.cell.held)
        if not SAYS_NOTHING.fullmatch(data):
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
        self.unsaid = self.off_line = self.part = self.unbroken = None

    def _mark_table(self) -> None:
        """What each cell says of itself that its heading cannot: whether it is
        blank and whether its whole column is, its kind, its column's place,
        rank and name, and a cut cell its whole value; a group's span; each
        row's open line; and the table's tracks and the widths it gives way
        at (``_layout``)."""

        heads = [row.cells for row in self.rows if row.section == "thead"]
        records = [row for row in self.rows if row.section == "tbody" and not row.group]
        kinds = _kinds(heads, records)
        # A table of row headers and no headings is a list of labels and
        # values: no column names its rows.
        named = -1 if not heads and kinds.get(0) == "status" else _kept_naming_column(heads)
        empty = _empty_columns(heads, named, records)
        width = _width(heads, records)
        names, drops, gutter = _columns(heads[-1] if heads else [])
        # The columns of what a row is (a cell said by its pills alone; a pill
        # beside a value is a note on it) and of its
        # controls, which stand beneath its words in a narrow box (tables.css)
        # and so never leave: a row without them no longer says what it is or
        # does what it is for.
        states = {
            place
            for row in records
            for place, cell in _placed(row)
            if cell.tops and all(cell.tops) and not cell.loose and place != named
        }
        beneath = states | {place for row in records for place, _ in _row_controls(row, kinds, named)}
        # A field a reader fills in is what the row is for too, kept in its column.
        fields = {place for row in records for place, cell in _placed(row) if cell.fields}
        ranked = {at: rank for at, rank in drops.items() if at not in beneath | fields | empty}
        # A heading keeps no rank its column does not, or the region lets it go alone.
        self.cuts += [
            cell.drop
            for row in self.rows
            if row.section == "thead"
            for at, cell in _placed(row)
            if cell.drop and at not in ranked
        ]
        if not (self.sorts and _sorts(records)):
            self.cuts += _sort_controls(heads)
        self.marks.setdefault(self.table_at, []).append(_layout(kinds, named, empty, ranked, beneath, width))
        # A row group drawn as `display: contents` loses the role its tag
        # gives it; a table, row or cell on a grid keeps its own.
        for start in self.sections:
            self.marks.setdefault(start, []).append(' role="rowgroup"')
        for row in self.rows:
            if row.group:
                self._span(row, width)
                continue
            self._mark_cells(row, kinds, named, empty, names, ranked)
            self._name(row, kinds, named)
            if gutter is not None and row.section != "thead":
                self._gutter(row, gutter)
            if row.section == "thead":
                for place in beneath:
                    if cell := _at_place(row.cells, place):
                        self.marks.setdefault(cell.start, []).append(" data-beneath")
            if row.section == "tbody" and row.end:
                stated = [
                    (at, cell)
                    for at, cell in _placed(row)
                    if at in states and cell.closed and "colspan" not in cell.attrs
                ]
                for cell in dict(stated + _row_controls(row, kinds, named)).values():
                    self.marks.setdefault(cell.start, []).append(" data-beneath")
                self._detail(row, width)

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

    def _mark_cells(
        self, row: _Row, kinds: dict[int, str], named: int, empty: set[int], names: list[str], drops: dict[int, int]
    ) -> None:
        """What each cell says of itself, in the body its kind, its column's
        rank and name, and a cut cell its whole value."""

        body = row.section == "tbody"
        for at, cell in _placed(row):
            marks = []
            if body and cell.says_nothing:
                marks.append(" data-blank")
            if "colspan" not in cell.attrs:
                marks += self._column_marks(
                    cell, at, kinds, named, empty, names, drops, body=body, head=row.section == "thead"
                )
            self.marks.setdefault(cell.start, []).extend(marks)

    def _column_marks(
        self,
        cell: _Cell,
        at: int,
        kinds: dict[int, str],
        named: int,
        empty: set[int],
        names: list[str],
        drops: dict[int, int],
        *,
        body: bool,
        head: bool,
    ) -> list[str]:
        """What a cell that stands in one column says of itself there."""

        marks = [" data-blank-column"] if at in empty else []
        if head:
            return marks
        kind = _kind_at(at, kinds, named)
        if "data-kind" not in cell.attrs:
            marks.append(f' data-kind="{kind}"')
        if at in drops and "data-drop" not in cell.attrs:
            marks.append(f' data-drop="{drops[at]}"')
        # Its column's name, for where it stands away from its heading: a
        # column let go shows in its open row, a field beneath the row's
        # words, as do the words a column of controls holds where a row has
        # no control.
        worded = body and kind == "control" and not cell.controls and not cell.says_nothing
        named_away = at in drops or cell.fields or worded
        if named_away and at < len(names) and names[at] and "data-label" not in cell.attrs:
            marks.append(f' data-label="{html.escape(names[at])}"')
        if body and kind in _CUT_KINDS and not cell.says_nothing:
            # A run that cannot break holds one line, so one longer than its
            # column's floor is cut there, as a line longer than LONG is.
            fits = int(float(TRACKS[kind][0].removesuffix("rem")) * _RUN_PER_REM) - _RUN_PADDING
            if cell.longest > LONG or cell.run > fits:
                marks.append(" data-long")
            said = " ".join("".join(cell.text).split())
            if said and "title" not in cell.attrs:
                marks.append(f' title="{html.escape(said)}"')
        if body and kind == "status" and cell.tops == [True] and not cell.loose:
            marks.append(" data-pill-only")
        return marks

    def _name(self, row: _Row, kinds: dict[int, str], named: int) -> None:
        """Mark what names the row, in each cell of words that leads with it:
        a held column's bold (a status said in bold) names nothing."""

        place = 0
        for cell in row.cells:
            at = place
            place += int(cell.attrs.get("colspan") or 1)
            if TRACKS[_kind_at(at, kinds, named)][1] == "hold":
                continue
            for start in cell.names:
                self.roles.setdefault(start, set()).add("name")

    def _detail(self, row: _Row, width: int) -> None:
        """The row's open line: the blocks it keeps beneath itself, then each
        labeled block under its label. A column the region let go shows in
        the row itself once it opens (tables.css)."""

        blocks: list[Span] = []
        labeled: list[LineEntry] = []
        held_below = False
        for cell in row.cells:
            if "colspan" in cell.attrs or not cell.closed:
                continue
            # The row's editor leads its line, moved there: its ids are its own.
            editors = [span for _, span in _finished(cell, "editor")]
            self.cuts += [((start, 0), (end, None)) for start, end in editors]
            blocks[:0] = editors
            # A block kept beneath the row is moved there too: the row's line
            # never shows it, and the row says it holds one (`data-below`).
            below = _finished(cell, "below")
            self.cuts += [((start, 0), (end, None)) for _, (start, end) in below]
            held_below = held_below or bool(below)
            blocks += [span for label, span in below if not label]
            labeled += [LineEntry(label, span) for label, span in below if label]
        if held_below:
            self.marks.setdefault(row.start, []).append(" data-below")
        if blocks or labeled:
            self.details.append((row.end, width, blocks, labeled))
        if any(_finished(cell, "editor") for cell in row.cells):
            self._open_row(row)

    def _span(self, row: _Row, width: int) -> None:
        """A group's heading row spans every column its table declares."""

        for cell in row.cells:
            marks = []
            if width and "colspan" not in cell.attrs:
                marks.append(f' colspan="{width}"')
            self.marks.setdefault(cell.start, []).extend(marks)


def _classes(attrs: dict[str, str | None]) -> list[str]:
    return (attrs.get("class") or "").split()


def _row_controls(row: _Row, kinds: dict[int, str], named: int) -> list[tuple[int, _Cell]]:
    """Each cell of the row's controls, by its place: a row's actions or a
    cell of a column of controls, holding a control or a field a reader fills
    in, which the row is for and so never leaves, or the words that stand
    where a row has none. Never the cell that names the row, nor a column of
    tick-boxes."""

    found = []
    for at, cell in _placed(row):
        classes = _classes(cell.attrs)
        if at == named or "colspan" in cell.attrs or "select-column" in classes:
            continue
        if not cell.closed:
            continue
        controls = "control" in (kinds.get(at), cell.attrs.get("data-kind")) or "row-actions" in classes
        if controls and (cell.controls or not cell.says_nothing):
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


def _columns(heading: list[_Cell]) -> tuple[list[str], dict[int, int], int | None]:
    """Each column's name, the rank of each column that may leave, and the
    one whose heading keeps the gutter."""

    names = [" ".join("".join(cell.text).split()) for cell in heading]
    drops = {index: int(cell.attrs["data-drop"]) for index, cell in enumerate(heading) if cell.attrs.get("data-drop")}
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


def _kinds(heads: list[list[_Cell]], body: list[_Row]) -> dict[int, str]:
    """Each column's kind, by place, from the heading that declares it. A
    table with no headings is read off its rows: a column of a row's actions
    is its controls, and a row's header is held to what it says."""

    if heads:
        return {
            index: kind for cells in heads for index, cell in enumerate(cells) if (kind := cell.attrs.get("data-kind"))
        }
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


# The integers a table's region is read by (tables.css): one per rank, 1
# while the region holds that rank's columns at their floors, and `--whole`,
# 1 while it holds every column the table keeps beside its controls.
RANKS = 6


def _layout(
    kinds: dict[int, str], named: int, empty: set[int], ranked: dict[int, int], beneath: set[int], width: int
) -> str:
    """The table's grid, written on it as custom properties alone: each
    column's track from its kind (``TRACKS``), and each width the region
    gives way at, the sum of the floors of the columns it draws there. A
    ranked column leaves while the region is narrower than its rank's sum
    (`--need-<rank>`, smallest rank first); the controls stand beneath the
    row's words while it is narrower than the floors of every column the
    table keeps (`--need-0`). A blank column draws no track."""

    if any(rank > RANKS for rank in ranked.values()):
        raise ValueError(f"A drop rank is 1 to {RANKS}.")
    tracks, floors = [], {}
    # The column a cell drawn across the row takes its padding from: the
    # naming one, else the first kept one that shares.
    widest = next(
        (
            at
            for at in [named, *range(width)]
            if at >= 0 and at not in empty and TRACKS[_kind_at(at, kinds, named)][1] == "share" and at not in ranked
        ),
        None,
    )
    for at in range(width):
        kind = _kind_at(at, kinds, named)
        floor, grows = TRACKS[kind]
        # A column that stands beneath the row's words in a narrow box is as
        # wide as its cells beside them, so once they leave it it is as wide
        # as its clipped heading: nothing.
        if at in beneath:
            grows = "hold"
        if kind == "name" and beneath:
            floor = f"max({floor}, {_BESIDE_CONTROLS})"
        if at in empty:
            tracks.append("0px")
            continue
        floors[at] = floor
        # A held column's values are short and never cut, so it is as wide as
        # they ask. The shared ones grow alike toward what their widest cells
        # ask from nothing, cutting what they hold where they get less
        # (cuts.css), so where the held columns ask more than their floors they
        # give way rather than the table overflow; the room left once each has
        # what it asks the kept ones share alike too (`auto`, which the grid
        # stretches). A ranked one grows no further than its asks, since the
        # grid stretches an `auto` track whose cells have left as well.
        # The widest column holds at least the padding of a cell drawn across
        # the row (`_SPANNING`), so that cell asks no held column for it.
        # A column of controls is as wide as its widest control and no wider:
        # the grid would widen it to the row with the most controls, leaving
        # every other row's words and columns short of the room beside them,
        # so a row with more than one control stands them in lines.
        if kind == "control":
            tracks.append("min-content")
            continue
        # A shared column keeps its floor while the table keeps it (`--whole`,
        # or its rank's `--keep-<rank>`, tables.css), which it does only while
        # it has room for every floor it keeps, so no column squeezes another
        # under its floor: the words beside the row's controls keep their line
        # (`_BESIDE_CONTROLS`), and a column beside them its words.
        least = "min-content" if grows == "hold" else _SPANNING if at == widest else "0px"
        if grows == "share":
            kept = f"var(--keep-{ranked[at]})" if at in ranked else "var(--whole)"
            least = f"max({least}, {floor} * {kept})"
        most = "max-content" if grows == "hold" or at in ranked else "auto"
        tracks.append(f"minmax({least}, {most})")
    needs = {0: [floor for at, floor in floors.items() if at not in ranked]}
    for rank in sorted(set(ranked.values())):
        needs[rank] = [floor for at, floor in floors.items() if ranked.get(at, rank) >= rank]
    said = [f"--tracks: {' '.join(tracks)}"]
    said += [f"--need-{rank}: calc({' + '.join(sum_)})" for rank, sum_ in needs.items() if sum_]
    return f' style="{html.escape("; ".join(said))}"'


_CUSTOM_PROPERTY = re.compile(r"\s*--[\w-]+\s*:")


def sets_only_custom_properties(style: str) -> bool:
    """Whether a style attribute only hands the stylesheet values it reads, as
    ``_layout``'s does, and so styles nothing from the markup: the one kind
    of style attribute HQ writes (test_rendered holds it to that)."""

    return all(_CUSTOM_PROPERTY.match(each) for each in style.split(";") if each.strip())


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
