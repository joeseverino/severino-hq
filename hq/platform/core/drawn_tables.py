"""A declared table (``tables.DataTable``) as ``partials/_data_table.html``
draws it: each heading and cell's facts read off its declaration, laid out
by ``table_layout.lay_out``, and every mark it gives written where the
template draws the element it belongs to. No markup is read back."""

from dataclasses import dataclass, field
from typing import Any

from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.tables import Cell, DataTable, Group, Row, column_kind
from hq.platform.core.held_values import format_html
from hq.platform.core.templatetags.table_tags import LINE_FRAME, table_heading
from hq.platform.core.templatetags.value_tags import money, when_text
from hq.platform.core.templatetags.write_tags import row_editor

from .table_layout import SAYS_NOTHING, CellFacts, Moved, RowFacts, TableFacts, lay_out

# The class a value that shows only part of the time is drawn in, which the
# region reads (``table_cells._OFF_LINE``, ``table_layout.HELD``).
SHOWN = {"more": "row-more", "narrow": "narrow-only", "narrow-more": "narrow-more"}
# The classes of a column its cells carry too: a moment keeps its line (tables.css).
_CELL_CLASSES = frozenset({"date-col"})
# What the row's line shows of a value drawn only once it opens or in a narrow box.
_OFF_LINE = frozenset({"more", "narrow"})
# The characters a dot before a value adds to its line: a no-break space and the dot.
_DOT = 2


@dataclass(slots=True)
class DrawnCell:
    """One cell as the template draws it: its declaration, its start tag's
    attributes, the box its value is held in, the toggle drawn last in it
    (whether its row is drawn open), and whether its bold names the row."""

    cell: Cell
    facts: CellFacts
    # What the value is to the stylesheet beyond its column's kind (``Cell.role``).
    role: str = ""
    attributes: SafeString = field(default_factory=SafeString)
    box: str = ""
    toggle: bool | None = None
    named: bool = False
    # The row's editor as ``{% row_editor %}`` answers it.
    editor: dict[str, Any] | None = None


@dataclass(slots=True)
class DrawnRow:
    row: Row
    facts: RowFacts
    cells: list[DrawnCell] = field(default_factory=list)
    attributes: SafeString = field(default_factory=SafeString)
    # Its open line: how many columns it spans, the editor it leads with, and
    # whether it lists the row's run evidence.
    detail: int = 0
    editor: dict[str, Any] | None = None
    # A cell for its tick-box and one for its actions, each with its marks.
    select: DrawnCell | None = None
    actions: DrawnCell | None = None


@dataclass(slots=True)
class DrawnGroup:
    """A group of rows, and how many columns its heading spans."""

    group: Group
    rows: list[DrawnRow]
    across: int


def drawn(table: DataTable, context: Any) -> dict[str, Any]:
    """What ``_data_table.html`` draws of ``table``."""

    headings = _headings(table)
    width = len(headings)
    groups = [
        DrawnGroup(group, [_row(table, row, context, width) for row in group.rows], width) for group in table.bodies
    ]
    facts = [RowFacts("thead", cells=[facts for _, facts in headings])]
    facts += [row.facts for group in groups for row in group.rows]
    laid = lay_out(facts, sorts=table.sorted_by is None)
    # A row across the table stands in the gutter its names keep, holding no toggle.
    gutter = any("row-gutter" in facts.added for _, facts in headings)
    for group in groups:
        for row in group.rows:
            if row.row.across and gutter:
                row.cells[0].facts.added.append("row-gutter")
            _mark(row, width)
    sorting = {"table": {"selected_sort": table.sorted_by or ""}}
    return {
        "table": table,
        "headings": [_heading(sorting, column, facts, laid) for column, facts in headings],
        "groups": groups,
        "marks": mark_safe("".join(laid.marks)),  # noqa: S308 - the layout's own attributes
        "rows": any(group.rows for group in groups),
        # The open line a region whose rows hold cells back draws once, which tables.js clones.
        "frame": mark_safe(LINE_FRAME) if laid.held else "",  # noqa: S308 - the region's own literal markup
    }


def _headings(table: DataTable) -> list[tuple[dict[str, Any], CellFacts]]:
    """Each heading as ``table_heading`` takes it, with its facts."""

    found: list[tuple[dict[str, Any], CellFacts]] = []
    if table.selectable:
        found.append(
            ({"label": "Select all rows", "select_all": True}, CellFacts(classes=("select-column",), kind="control"))
        )
    for column in table.columns:
        label = column.label or "Actions"
        found.append(
            (
                {
                    "label": label,
                    "kind": column.kind,
                    "css": column.css,
                    "sort": column.sort,
                    "hidden": not column.label,
                },
                CellFacts(
                    classes=tuple(column.css.split()), kind=column_kind(column.kind, column.css), drop=column.drop
                ),
            )
        )
    if table.has_actions:
        found.append(
            (
                {"label": "Actions", "css": "actions-col", "hidden": True, "kind": "control"},
                CellFacts(classes=("actions-col",), kind="control"),
            )
        )
    return found


def _heading(sorting: dict[str, Any], column: dict[str, Any], facts: CellFacts, laid: TableFacts) -> dict[str, Any]:
    """What ``_table_heading.html`` draws of a heading: its marks (the level
    it leaves at for its rank among them), and a sort on the page only where
    its rows can be put in order."""

    css = " ".join([*column.get("css", "").split(), *facts.added])
    drawn = table_heading(sorting, **{**column, "css": css}, local_sort=None if laid.sorts else False)
    return {**drawn, "marks": mark_safe("".join(facts.marks))}  # noqa: S308 - the layout's own attributes


def _row(table: DataTable, row: Row, context: Any, width: int) -> DrawnRow:
    cells = [_cell(cell, context) for cell in row.cells]
    for drawn_cell, column in zip(cells, table.columns, strict=False):
        drawn_cell.facts.classes = tuple(name for name in column.css.split() if name in _CELL_CLASSES)
    if row.across and cells:
        cells[0].facts.colspan, cells[0].facts.spans = width, True
    drawn_row = DrawnRow(row, RowFacts("tbody", cells=[cell.facts for cell in cells]), cells)
    # A row of one cell across the table spans its tick-box's column and its actions' too.
    if row.across:
        return drawn_row
    if table.selectable:
        ticked = row.key is not None
        drawn_row.select = DrawnCell(
            Cell(), CellFacts(classes=("select-column",), says_nothing=not ticked, controls=ticked, fields=ticked)
        )
        drawn_row.facts.cells.insert(0, drawn_row.select.facts)
    if table.has_actions:
        drawn_row.actions = DrawnCell(Cell(), _actions(row))
        drawn_row.facts.cells.append(drawn_row.actions.facts)
    if row.evidence and drawn_row.facts.cells:
        drawn_row.facts.cells[-1].moved.append(Moved("below", "evidence", row.evidence))
    drawn_row.editor = next((cell.editor for cell in cells if cell.editor and cell.editor["open"]), None)
    return drawn_row


def _actions(row: Row) -> CellFacts:
    return CellFacts(
        classes=("row-actions",), says_nothing=not row.actions, controls=bool(row.actions), presses=bool(row.actions)
    )


def _mark(row: DrawnRow, width: int) -> None:
    """Write the marks the layout gave the row and its cells."""

    for cell in (row.select, *row.cells, row.actions):
        if cell is None:
            continue
        if row.row.across:
            cell.facts.marks.insert(0, f' colspan="{width}"')
        cell.attributes = _attributes(cell.facts, cell.role)
        cell.box, cell.toggle, cell.named = cell.facts.box, cell.facts.toggle, cell.facts.named
    attributes = [*row.facts.marks]
    if row.row.folds:
        attributes.append(format_html(' id="{}" data-folds', row.row.folds))
    if row.row.folded:
        attributes.append(format_html(' data-folded="{}"', row.row.folded))
    if row.row.emphasized:
        attributes.append(" data-emphasized")
    if row.facts.added:
        attributes.insert(0, format_html(' class="{}"', " ".join(row.facts.added)))
    row.attributes = mark_safe("".join(attributes))  # noqa: S308 - each piece escaped above or the layout's own
    if row.facts.detail:
        row.detail = row.facts.detail[0]


def _attributes(facts: CellFacts, role: str) -> SafeString:
    classes = [*facts.classes, *facts.added]
    return format_html(
        "{}{}{}",
        format_html(' class="{}"', " ".join(classes)) if classes else "",
        format_html(' data-cell="{}"', role) if role else "",
        mark_safe("".join(facts.marks)),  # noqa: S308 - the layout's own attributes
    )


def _cell(cell: Cell, context: Any) -> DrawnCell:
    """A cell's facts, read off its declaration."""

    editor = None
    if cell.editor is not None:
        asked = cell.editor
        editor = row_editor(
            context, asked.form, asked.key, label=asked.label, action=asked.action, verb=asked.verb, **asked.initial
        )
    parts = [part for part in cell.parts if part.shows not in _OFF_LINE]
    on_line = [_length(cell), *(_length(part) for part in parts)]
    line = sum(on_line) + (len(str(cell.count)) if cell.count is not None else 0) + cell.fold
    lines = [sum(_length(part) for part in (each, *each.parts) if part.shows not in _OFF_LINE) for each in cell.lines]
    tops = [bool(cell.tone)] + [False] * (len(cell.parts) + bool(cell.lines) + (cell.count is not None) + cell.fold)
    facts = CellFacts(
        says_nothing=cell.says_nothing,
        longest=max([line, *lines]),
        controls=bool(cell.url or (cell.link and cell.link.url) or cell.fold or cell.field is not None or editor),
        fields=cell.field is not None,
        moved=[Moved("editor", None, editor)] if editor and editor["open"] else [],
        holds={SHOWN[part.shows] for part in (cell, *cell.parts, *cell.lines) if part.shows in ("more", "narrow-more")},
        names=int(cell.name),
        parted=any(part.role for part in (*cell.parts, *cell.lines)),
        toggled=cell.fold,
        folds=cell.fold,
        tops=tops,
        said=[not cell.says_nothing] + [True] * (len(tops) - 1),
        loose=False,
    )
    return DrawnCell(cell, facts, role=cell.role, editor=editor)


def _length(cell: Cell) -> int:
    """The characters a value shows on its line, spaces collapsed."""

    if cell.moment is not None:
        words = when_text(cell.moment, cell.form if cell.form != "mark" else "")
    elif cell.amount is not None:
        words = str(money(cell.amount))
    else:
        words = (cell.link.label if cell.link else cell.text) + cell.after
    shown = 0 if SAYS_NOTHING.fullmatch(words) else len(" ".join(words.split()))
    return shown + (_DOT if cell.dot else 0)
