"""Where a table's columns leave, derived from what each holds.

A column stands at its floor: the least width the browser lays it out at, at
its min-content, as the stylesheet sets what it holds (`css/app/tables.css`).
Some of what a cell holds out for is bounded by its column's even share of
the region (`--hold`), so a floor is read at a width. Each ranked column, in
rank order, leaves below the least declared width its table's columns still
showing fit at their floors; `dev:look --rule floors` sets each floor beside
the browser's own min-content for its column.
"""

import math
from dataclasses import dataclass

# The region widths, in CSS pixels, a column may leave at, about a tenth
# apart and up to the page's `--content-max`. `manage.py table_steps` writes
# the stylesheet's rule for each.
DROP_STEPS = (280, 320, 360, 400, 440, 480, 540, 600, 660, 720, 800, 880, 960, 1040, 1120, 1200, 1300, 1400, 1500, 1600)
# The most columns a table's share of its region counts.
MOST_COLUMNS = 16

# What the stylesheet sets a cell in, in CSS pixels: its inline padding
# (`--space-6` either side); the gutter a row's toggle stands in, which the
# region's share is taken past (`--row-gutter`), and what it adds to the cell
# that names the row past the padding it replaces; and the width that cell
# holds out for when it leads with a name, at most two fifths of the region
# (figures.css, `11rem`).
_PADDING = 24
_ROW_GUTTER = 36
_GUTTER = 24
_NAME = 176
_NAME_PART = 0.4
# How many shares the cell that names the row holds out for (tables.css).
_NAME_SHARES = 1.5
# Inter's `ch` (the advance of "0"), in ems, which the stylesheet's floors
# are given in, and the size, in pixels, a cell's text is set at (`--text-sm`).
CH = 0.62
CELL_TEXT = 13
# In characters: what a line that wraps holds out for (`data-long`), in `ch`;
# the length a value the stylesheet cuts holds out for (`--cut-floor`), and a
# name it holds whole (`--name-hold`). A line longer than three prose floors
# wraps, held out to one.
_PROSE = 16
CUT = 12
NAME_HOLD = 28
LONG = 3 * _PROSE


@dataclass(frozen=True, slots=True)
class ColumnText:
    """What a column holds, in CSS pixels as the stylesheet sets it.

    ``line`` is the longest line of plain words it keeps, ``parts`` the
    longest of parts of its own, and ``pinned`` the longest that holds a pill
    or a time; ``held`` is its longest run with no
    break in it that the stylesheet holds out for only so far (a cut value, a
    name), ``whole`` the longest it holds whole, and ``label`` its heading's.
    ``long`` says a line of it is too long to keep, and wraps.
    """

    kind: str = "prose"
    line: float = 0
    parts: float = 0
    pinned: float = 0
    held: float = 0
    whole: float = 0
    label: float = 0
    long: bool = False
    names_row: bool = False
    leads: bool = False
    gutter: bool = False

    def floor(self, width: int, share: float) -> int:
        """The column's floor in a region ``width`` wide, each column's even
        share of it ``share``: never narrower than its longest run or its
        heading. A control keeps its whole line, as does a status or a figure
        of plain words or with a pill or a time in it; one with parts of its
        own keeps it up to the share. A line too long to keep, of prose or a
        status, holds out for the prose floor up to the share, and any other
        wraps wherever it breaks. A cell that names the row and leads with a
        name holds out for a name's width, at most two fifths of the region
        and a share and a half."""

        hold = share - _PADDING
        least = max(self.whole, self.held, self.label)
        if self.kind == "control":
            least = max(least, self.line)
        elif self.kind in ("status", "numeric"):
            least = max(least, self.pinned, self.line, min(self.parts, hold))
        if self.kind in ("prose", "status") and self.long:
            least = max(least, min(_PROSE * CH * CELL_TEXT, hold))
        floor = least + _PADDING + (_GUTTER if self.gutter else 0)
        if self.leads:
            floor = max(floor, min(_NAME, _NAME_PART * width, _NAME_SHARES * share))
        return math.ceil(floor)


@dataclass(frozen=True, slots=True)
class FitColumn:
    """What a table's fit reads of one column: what it holds, or the width it
    stands at whatever it holds (a column of tick-boxes, a blank one); its
    rank (0 stays); and whether it counts toward the share of the region each
    column holds out for (a column of tick-boxes and a blank one do not)."""

    text: ColumnText | int
    rank: int = 0
    shares: bool = True

    def floor(self, width: int, share: float) -> int:
        return self.text if isinstance(self.text, int) else self.text.floor(width, share)


@dataclass(frozen=True, slots=True)
class Fit:
    """Where a table's columns leave, derived from what they hold."""

    # The width each ranked column leaves below, by place.
    leaves: dict[int, int]
    # Below it the table shows only what it keeps.
    narrow: int
    # How many columns share the region: at full width, then below each
    # width a column leaves at, the widest first.
    columns: int
    columns_below: tuple[tuple[int, int], ...]


def floors_at(columns: tuple[FitColumn, ...], showing: set[int], width: int) -> dict[int, int]:
    """The floor of each column showing, by place, in a region ``width``
    wide: its share counts the columns showing that share, and one more for
    the name's half share and what holds out unbounded (tables.css)."""

    sharing = min(sum(columns[index].shares for index in showing), MOST_COLUMNS)
    share = (width - _ROW_GUTTER) / (sharing + 1)
    return {index: columns[index].floor(width, share) for index in showing}


def least_step(columns: tuple[FitColumn, ...], showing: set[int]) -> int:
    """The narrowest declared width the columns showing fit at their floors,
    or the widest when none does."""

    return next(
        (step for step in DROP_STEPS if sum(floors_at(columns, showing, step).values()) <= step),
        DROP_STEPS[-1],
    )


def fit(columns: tuple[FitColumn, ...]) -> Fit:
    """Each ranked column, in rank order, leaves below the least declared
    width every column still showing fits at; columns of one rank leave
    together. Below the width the last leaves at, or in a table where none
    may, below the width what it keeps fits at, the table is in a narrow box."""

    showing = set(range(len(columns)))
    whole = sum(column.shares for column in columns)
    leaves: dict[int, int] = {}
    below: dict[int, int] = {}
    for rank in sorted({column.rank for column in columns} - {0}):
        width = least_step(columns, showing)
        leaving = {index for index, column in enumerate(columns) if column.rank == rank}
        leaves |= dict.fromkeys(leaving, width)
        showing -= leaving
        below[width] = sum(columns[index].shares for index in showing)
    return Fit(
        leaves=leaves,
        narrow=min(leaves.values(), default=least_step(columns, showing)),
        columns=min(whole, MOST_COLUMNS),
        columns_below=tuple((width, min(count, MOST_COLUMNS)) for width, count in below.items()),
    )
