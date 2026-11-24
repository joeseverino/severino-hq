"""What the stylesheet reads off a table, decided from what each cell holds.

A table's facts (``RowFacts`` of ``CellFacts``) come from its declaration
(``tables.DataTable``, by ``drawn_tables``). ``lay_out`` decides from them every mark the
stylesheet reads: blank cells and columns, each column's kind and the width
it leaves at, clamped and short values, the row's name, the gutter and its
toggles, and each row's open line. The stylesheet never reads a cell's
content.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from typing import Any

# A value that says nothing: dashes and space, and nothing drawn but the
# empty value's own mark.
SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")
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
HELD = frozenset({"row-more", "line-rest", "narrow-more"})
# What a cell holds that its row's line cuts and its open line releases in place.
GIST = "row-gist"
# How many widths of its region a table's columns leave at, one rule each in
# tables.css, the narrowest wider than a narrow box (36rem), so a narrow box
# holds only the columns a row keeps.
RANKS = 4
# The width below every rank's (20rem, tables.css), where a row's state, which
# stands while any other column can, moves to its open line too.
STATES_LEAVE = RANKS + 1
# How many columns at most a table's share is counted over (`data-columns`),
# one rule each in tables.css.
COUNTED = 10
# How many columns at most a table draws, its row's controls among them: the
# tracks of its grid (tables.css) after a row's tick-box, which leads.
TRACKS = 16


@dataclass(slots=True)
class Moved:
    """What a cell moves to its row's open line: a block kept beneath the
    row (``below``) or the row's editor (``editor``), by its label, and
    ``content``, what is drawn there."""

    kind: str
    label: str | None
    content: Any = None


@dataclass(slots=True)
class CellFacts:
    """What one cell holds, as the layout reads it, and the marks it is given."""

    # Its own attributes: its span, classes, kind, a heading's rank, a row header.
    colspan: int = 1
    spans: bool = False
    classes: tuple[str, ...] = ()
    kind: str = ""
    drop: int = 0
    row_header: bool = False
    # A heading's words, which a cell under it standing apart in a narrow box is named by.
    label: str = ""
    says_nothing: bool = True
    # The characters on its longest line, spaces collapsed.
    longest: int = 0
    # Whether it holds a control, a field a reader fills in, and a button.
    controls: bool = False
    fields: bool = False
    presses: bool = False
    moved: list[Moved] = field(default_factory=list)
    # What it holds that its row shows only on its open line (``HELD``).
    holds: set[str] = field(default_factory=set)
    # How many elements name the row: a leading bold, or the bold in a leading link.
    names: int = 0
    # Whether what it shows on its row's line carries a part's role (`data-cell`).
    parted: bool = False
    # Whether it holds the toggle that opens a folded group.
    folds: bool = False
    # Whether each value it shows is a pill, and whether each says a word: a
    # cell said by its pills alone is what a row is (`states`).
    tops: list[bool] = field(default_factory=list)
    said: list[bool] = field(default_factory=list)

    # What the layout gives it: what is written in its start tag, the classes
    # added to its own, the box its value is held in, the toggle drawn last
    # in it (whether its row is drawn open), and whether its leading bold
    # names the row.
    marks: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    box: str = ""
    toggle: bool | None = None
    named: bool = False

    @property
    def states(self) -> bool:
        pills = [pill for pill, said in zip(self.tops, self.said, strict=True) if pill or said]
        return bool(pills) and all(pills)


@dataclass(slots=True)
class RowFacts:
    """One row, and what the layout gives it: its marks, its classes, and its
    open line (the blocks it shows whole, then each labeled entry)."""

    section: str
    cells: list[CellFacts] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    detail: tuple[int, list[Moved], list[Moved]] | None = None


@dataclass(slots=True)
class TableFacts:
    """What the layout gives a table: what its start tag says, whether its
    rows sort on the page, whether a row holds back a cell tables.js copies
    onto its open line (so the table draws the frame it clones), and the
    level each column leaves at."""

    marks: list[str] = field(default_factory=list)
    sorts: bool = True
    held: bool = False
    levels: dict[int, int] = field(default_factory=dict)


def lay_out(rows: list[RowFacts], *, sorts: bool = True) -> TableFacts:
    """What each cell says of itself that its heading cannot: whether it is
    blank and whether its whole column is, its kind and the width its
    column leaves at, and where a long one is clamped; a group's span; and
    each row's open line."""

    table = TableFacts()
    heads = [row.cells for row in rows if row.section == "thead"]
    records = [row for row in rows if row.section == "tbody"]
    kinds = _kinds(heads, records)
    # A table of row headers and no headings is a list of labels and
    # values: no column names its rows.
    named = -1 if not heads and kinds.get(0) == "status" else _kept_naming_column(heads)
    empty = _empty_columns(heads, named, records)
    longs = {place for row in records for place, cell in _placed(row) if not cell.spans and _long(cell)}
    width = _width(heads, records)
    # How many columns stand while every one does, each column's share
    # (tables.css): its last row of headings, or with none its widest
    # row, less the columns with nothing in any row.
    counted = heads[-1] if heads else max((row.cells for row in records), key=len, default=[])
    if (columns := min(COUNTED, sum(index not in empty for index in range(len(counted))))) > 1:
        table.marks.append(f' data-columns="{columns}"')
    # Of those, the ones a narrow box does not draw (`wide-only`), and the
    # columns of a row's choices, which it stands each on a line of its own
    # beneath the row's name (tables.css): its share is counted over neither.
    wide = {at for at, cell in enumerate(heads[-1] if heads else []) if "wide-only" in cell.classes}
    fielded = _fielded(heads[-1] if heads else [], kinds, named, records)
    if columns > 1 and (narrow := min(columns - 1, len((wide | set(fielded)) - empty))):
        table.marks.append(f' data-wide-columns="{narrow}"')
    drops = _drops(heads[-1] if heads else [])
    # The columns no heading's rank takes away: what a row is (a cell said
    # by its pills alone; a pill beside a value is a note on it), what it
    # does (its controls, a field a reader fills in, a button), and words
    # every row must show (`key-col`). Controls never leave, since the
    # open line copies what leaves, where a control would be drawn twice.
    # A key (`key-col`, or the closing figure of a table that marks none)
    # never leaves. A state (a cell said by its pills, or any under a
    # heading of kind `status` that declares no rank) stands while any other column can, and
    # goes to the open line only below every rank's width
    # (`STATES_LEAVE`), where the name, the keys and the controls take
    # the row.
    heading = heads[-1] if heads else []
    # A heading of kind `status` decides: with no rank its column is a
    # state, and with one it leaves by it, whatever its cells hold, though
    # never before another ranked column (`declared`).
    statuses = {at for at, cell in enumerate(heading) if cell.kind == "status" and at != named}
    declared = statuses & set(drops)
    states = {place for row in records for place, cell in _placed(row) if place != named and cell.states} - declared
    states |= statuses - declared
    keys = set(_keys([(kinds.get(at, ""), cell.classes) for at, cell in enumerate(heading)]))
    does = {place for row in records for place, cell in _placed(row) if cell.fields}
    does |= {place for row in records for place, _ in _row_controls(row, kinds, named)}
    pressed = {place for row in records for place, cell in _placed(row) if cell.presses}
    kept = states | keys | does
    ranked = {at: rank for at, rank in drops.items() if at not in kept | empty | declared}
    # Each rank is a width of the region (tables.css), counted from the
    # widest: a table's first rank leaves at the widest, and its others
    # each at the next width down, the ranks past the last width leaving
    # with it. Nothing is measured, so a column may leave a box that could
    # still hold it.
    order = sorted(set(ranked.values()))
    levels = {at: min(RANKS, order.index(rank) + 1) for at, rank in ranked.items()}
    if heads:
        levels |= _narrow_levels(heading, kinds, kept | pressed | empty | {named} | set(drops), after=len(order))
        levels |= dict.fromkeys(states - keys - does - pressed - empty - {named}, STATES_LEAVE)
    # A ranked status leaves with the last column that leaves before every
    # state does, so a row never shows a spare column without its status.
    last = max((level for level in levels.values() if level <= RANKS), default=1)
    levels |= dict.fromkeys(declared - kept - empty, last)
    table.levels = levels
    # A table whose rows can hold anything back (a column that leaves, a
    # value a narrow box clamps, a part only the open line shows, a fold)
    # keeps the gutter in the column that names each row, so every such
    # row carries the toggle that opens what left.
    # A table with no headings keeps it in its first column, where each row is named.
    holds = any(_holds_back(row, kinds, named) for row in records)
    gutter = (named if levels or holds else None) if heads else (0 if holds else None)
    opens = False
    table.sorts = sorts and _sorts(records)
    for row in rows:
        # A value is clamped only where its row's toggle opens it whole.
        toggle_cell = None if gutter is None else _at_place(row.cells, gutter)
        toggled = toggle_cell is not None and not toggle_cell.folds
        _mark_cells(row, kinds, named, empty, levels, wide, fielded, toggled=toggled)
        if row.section == "thead":
            _mark_long_heads(row, longs)
        _name(row, kinds, named)
        if gutter is not None:
            _gutter(row, gutter)
        if row.section == "tbody":
            shown, held = _detail(row, width, levels)
            opens = opens or shown
            table.held = table.held or held
    if opens:
        table.marks.append(" data-rows-can-open")
    return table


def _gutter(row: RowFacts, place: int) -> None:
    """The cell of a row in the column that keeps the gutter stands in
    the gutter, and a record's holds the toggle that opens it, last in
    the cell."""

    cell = _at_place(row.cells, place)
    if cell is None:
        return
    if "row-gutter" not in cell.classes:
        cell.added.append("row-gutter")
    if row.section != "tbody" or cell.folds:
        return
    cell.toggle = any(_moved(each, "editor") for each in row.cells)


def _mark_cells(
    row: RowFacts,
    kinds: dict[int, str],
    named: int,
    empty: set[int],
    levels: dict[int, int],
    wide: set[int],
    fielded: dict[int, str],
    *,
    toggled: bool,
) -> None:
    """What each cell says of itself: blank, its whole column blank, the
    width its column leaves at, that it stands only in a wide box as its
    heading does (`wide-only`), that its column holds a field (`data-field`)
    and in the body the heading's words, and in the body its kind and its
    clamp."""

    body = row.section == "tbody"
    for at, cell in _placed(row):
        marks = [" data-blank"] if body and cell.says_nothing else []
        if not cell.spans:
            marks += [" data-blank-column"] if at in empty else []
            marks += [f' data-drop="{levels[at]}"'] if at in levels else []
            marks += [" data-wide-only"] if body and at in wide else []
            if at in fielded:
                marks.append(f' data-field data-label="{escape(fielded[at])}"' if body else " data-field")
            if row.section != "thead":
                marks += _body_marks(cell, _kind_at(at, kinds, named), body=body, toggled=toggled)
        cell.marks.extend(marks)


def _mark_long_heads(row: RowFacts, longs: set[int]) -> None:
    """A heading over a column holding a long value says so: its column
    shares the room, whatever its kind."""

    for at, cell in _placed(row):
        if at in longs and not cell.spans:
            cell.marks.append(" data-long")


def _body_marks(cell: CellFacts, kind: str, *, body: bool, toggled: bool) -> list[str]:
    """What a body cell says of itself under its column."""

    marks = [] if cell.kind else [f' data-kind="{kind}"']
    if body and _long(cell):
        marks.append(" data-long")
    if body and _words(cell, kind) and not cell.says_nothing:
        if _long(cell):
            if toggled and _clamps(kind):
                _box(cell, kind, "cell-clamp")
        # A fold's toggle is drawn before its head's value, which the
        # value's span (ended at a toggle) would leave out, and a field
        # fills its column, which a short value's box would narrow, as it
        # would the room a gist takes on its line.
        elif cell.longest <= SHORT and not (cell.folds or cell.fields or GIST in cell.holds):
            _box(cell, kind, "cell-short")
    # A state is one box (`cell-state`, tables.css) that asks its column for
    # its line up to its share and wraps past it, where it holds one item: a
    # value drawn whole in it.
    elif body and kind == "status" and not (cell.says_nothing or _long(cell) or cell.controls or cell.fields):
        cell.box = "cell-state"
    return marks


def _box(cell: CellFacts, kind: str, mark: str) -> None:
    """Hold the cell's value in its clamp or its short value's box. A
    clamp holds each part with a role on its own (cuts.css), so around
    parts the cell is the clamp."""

    if mark == "cell-clamp" and (cell.parted or (cell.names and kind not in _HELD_KINDS)):
        cell.added.append(mark)
    else:
        cell.box = mark


def _name(row: RowFacts, kinds: dict[int, str], named: int) -> None:
    """Mark what names the row, in each cell of words that leads with it:
    a held column's bold (a status said in bold) names nothing."""

    for at, cell in _placed(row):
        if _kind_at(at, kinds, named) not in _HELD_KINDS and cell.names:
            cell.named = True


def _detail(row: RowFacts, width: int, levels: dict[int, int]) -> tuple[bool, bool]:
    """The row's open line as the server draws it, each moved block under
    its label; and whether the row can open, and holds back a cell.

    What a cell holds back is written once, in the cell, and tables.js
    copies it onto the line, so such a cell holds no control, which a copy
    would draw twice.
    """

    blocks: list[Moved] = []
    entries: list[Moved] = []
    always = opens = held = False
    for at, cell in _placed(row):
        if cell.spans:
            continue
        # The row's editor leads its line, moved there: its ids are its own.
        blocks[:0] = _moved(cell, "editor")
        moved = _moved(cell, "below")
        blocks += [part for part in moved if not part.label]
        entries += [part for part in moved if part.label]
        shown = bool(cell.holds & {"row-more", "line-rest"}) and not cell.says_nothing
        if (shown or at in levels) and not cell.says_nothing:
            if cell.presses or cell.fields:
                raise ValueError(
                    "A cell that holds something back for its row's open line holds no control: "
                    "it stands in place or moves there whole (row-below)."
                )
            held = True
        always = always or bool(moved) or shown
        opens = opens or shown or at in levels or bool({"narrow-more", GIST} & cell.holds)
    if always:
        row.marks.append(" data-below")
    if blocks or entries:
        row.detail = (width, blocks, entries)
    if any(_moved(cell, "editor") for cell in row.cells):
        # The row is open: its editor stands open on the line beneath it.
        row.added.append("is-expanded")
    return opens, held


def _long(cell: CellFacts) -> bool:
    """Whether a line of the cell runs longer than a line of words
    (``data-long``): what it says wraps, and its column shares the room."""

    return cell.longest > LONG and not cell.says_nothing


def _row_controls(row: RowFacts, kinds: dict[int, str], named: int) -> list[tuple[int, CellFacts]]:
    """Each cell of the row's controls, by its place: a row's actions or a
    cell of a column of controls, holding a control or the words that stand
    where a row has none. Never the cell that names the row, a column of
    tick-boxes, nor a field a reader fills in, which is the row's value."""

    found = []
    for at, cell in _placed(row):
        if at == named or cell.spans or "select-column" in cell.classes:
            continue
        controls = "control" in (kinds.get(at), cell.kind) or "row-actions" in cell.classes
        if controls and not cell.fields and (cell.controls or not cell.says_nothing):
            found.append((at, cell))
    return found


def _placed(row: RowFacts) -> list[tuple[int, CellFacts]]:
    """Each cell of the row with the column it starts at, past the spans before it."""

    found, place = [], 0
    for cell in row.cells:
        found.append((place, cell))
        place += cell.colspan
    return found


def _kept_naming_column(heads: list[list[CellFacts]]) -> int:
    """The column that names each row, which never leaves."""

    named = _naming_column(heads)
    if rank := next((cell.drop for cells in heads[-1:] for cell in cells[named : named + 1] if cell.drop), None):
        raise ValueError(f"The column that names each row has no drop rank, not {rank}.")
    return named


def leaving_order(columns: Sequence[tuple[str, Sequence[str]]]) -> list[int]:
    """The places of a table's columns, each by its kind and its classes, in
    the order a narrow table lets them go: ``optional-col`` columns first,
    then the rest, each from the far end. Never the first, which names the
    row, a ``key-col``, a column of the row's controls, the closing figure of
    a table that marks no key, or a date (``date-col``), which stands as a
    row's state does unless its table ranks it."""

    keys = _keys(columns)

    def spare(index: int, kind: str, classes: Sequence[str]) -> bool:
        return index > 0 and index not in keys and kind != "control" and "date-col" not in classes

    far_first = [index for index in reversed(range(len(columns))) if spare(index, *columns[index])]
    return sorted(far_first, key=lambda index: "optional-col" not in columns[index][1])


def _keys(columns: Sequence[tuple[str, Sequence[str]]]) -> list[int]:
    """The places of the columns every row keeps, each by its kind and its
    classes: its ``key-col`` ones, or with none the closing figure, the last
    column before the row's controls when it holds a number."""

    if keys := [index for index, (_, classes) in enumerate(columns) if "key-col" in classes]:
        return keys
    closing = next((index for index in reversed(range(len(columns))) if columns[index][0] != "control"), None)
    return [closing] if closing and columns[closing][0] == "numeric" else []


def _fielded(heading: list[CellFacts], kinds: dict[int, str], named: int, records: list[RowFacts]) -> dict[int, str]:
    """The heading's words of each column of a row's choices: a column of
    controls whose cells each hold a field a reader fills in, the words that
    stand where one cannot be, or nothing, never a button or a link."""

    found = {}
    for place, head in enumerate(heading):
        if place == named or kinds.get(place) != "control" or "select-column" in head.classes:
            continue
        cells = [cell for row in records if (cell := _at_place(row.cells, place))]
        if any(cell.fields or not cell.says_nothing for cell in cells) and not any(
            cell.controls and not cell.fields for cell in cells
        ):
            found[place] = head.label
    return found


def _narrow_levels(heading: list[CellFacts], kinds: dict[int, str], kept: set[int], *, after: int) -> dict[int, int]:
    """The levels of the columns a table's headings rank none for: each
    column a row can do without (`leaving_order`) leaves at the narrowest
    widths, after every ranked one (``after`` levels), the first to go at the
    widest of them and the last at the narrowest rank's width, so a table
    stands whole until a narrow box and keeps only what a row keeps there."""

    columns = [(kinds.get(at, ""), cell.classes) for at, cell in enumerate(heading)]
    spare = [at for at in leaving_order(columns) if at not in kept]
    return {at: min(RANKS, max(after + 1, RANKS - len(spare) + place)) for place, at in enumerate(spare, 1)}


def _drops(heading: list[CellFacts]) -> dict[int, int]:
    """The rank of each column that may leave."""

    return {index: cell.drop for index, cell in enumerate(heading) if cell.drop}


def _words(cell: CellFacts, kind: str) -> bool:
    """Whether the cell says words that may be cut: a value of a kind that
    wraps, or what a cell of controls says where it holds none."""

    return kind in _CUT_KINDS or (kind == "control" and not cell.controls)


def _clamps(kind: str) -> bool:
    """Whether a long value of the kind is held to a few lines: the name a
    row is read by wraps whole, as a part the row is read by does
    (`cell-whole`, cuts.css), so it never makes its row open."""

    return kind != "name"


def _holds_back(row: RowFacts, kinds: dict[int, str], named: int) -> bool:
    """Whether the row can hold back from its line what its open line shows:
    a long value a narrow box clamps, a part held for the open line, a block
    moved there, its editor, or a fold."""

    for at, cell in _placed(row):
        if cell.folds or cell.holds & (HELD | {GIST}) or _moved(cell, "below") or _moved(cell, "editor"):
            return True
        kind = _kind_at(at, kinds, named)
        if not cell.spans and _long(cell) and _words(cell, kind) and _clamps(kind):
            return True
    return False


def _sorts(records: list[RowFacts]) -> bool:
    """Whether rows sort on the page: where there are two to put in order. A
    row that spans the table holds its place (tables.js, sortRows)."""

    return sum(not any(cell.spans for cell in row.cells) for row in records) >= 2


def _at_place(cells: list[CellFacts], place: int) -> CellFacts | None:
    """The cell that starts at column ``place``, counting each cell's span;
    None when one spans across it or the row is shorter."""

    at = 0
    for cell in cells:
        if at == place:
            return None if cell.spans else cell
        at += cell.colspan
    return None


def _moved(cell: CellFacts, kind: str) -> list[Moved]:
    """Each part of ``kind`` the cell moves to its row's open line."""

    return [part for part in cell.moved if part.kind == kind]


def _kinds(heads: list[list[CellFacts]], body: list[RowFacts]) -> dict[int, str]:
    """Each column's kind, by place, from the heading that declares it. A
    column of a row's actions is its controls whatever its heading says, so
    they keep one line. A table with no headings is read off its rows, where
    a row's header is held to what it says."""

    actions = {place for row in body for place, cell in _placed(row) if "row-actions" in cell.classes}
    if heads:
        declared = {index: cell.kind for cells in heads for index, cell in enumerate(cells) if cell.kind}
        return declared | dict.fromkeys(actions, "control")
    kinds: dict[int, str] = {}
    for row in body:
        for index, cell in enumerate(row.cells):
            if "row-actions" in cell.classes or cell.kind == "control":
                kinds[index] = "control"
            elif cell.row_header:
                kinds.setdefault(index, "status")
    return kinds


def _kind_at(at: int, kinds: dict[int, str], named: int) -> str:
    """The kind of the column at ``at``: the naming column's own, else its heading's."""

    return "name" if at == named else kinds.get(at, "prose")


def _width(heads: list[list[CellFacts]], body: list[RowFacts]) -> int:
    """How many columns the headings declare, or in a table with none its widest
    row: what a group's heading row spans."""

    rows = heads or [row.cells for row in body]
    return max((sum(cell.colspan for cell in cells) for cells in rows), default=0)


def _naming_column(heads: list[list[CellFacts]]) -> int:
    """The place of the column that names the row: the first, or the second
    after a column of tick-boxes."""

    return int(any(cells and "select-column" in cells[0].classes for cells in heads))


def _empty_columns(heads: list[list[CellFacts]], named: int, body: list[RowFacts]) -> set[int]:
    """The columns with a heading and nothing in any row. Never the column that
    names the row."""

    width = max((len(cells) for cells in heads), default=0)
    empty = set()
    for index in range(named + 1, width):
        cells = [row.cells[index] for row in body if index < len(row.cells)]
        if cells and all(cell.says_nothing for cell in cells):
            empty.add(index)
    return empty
