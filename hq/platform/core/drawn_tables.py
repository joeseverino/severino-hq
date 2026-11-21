"""A declared table (``tables.DataTable``) as ``partials/_data_table.html``
draws it: each heading and cell's facts read off its declaration, laid out
by ``table_layout.lay_out``, and every mark it gives written where the
template draws the element it belongs to. No markup is read back."""

from dataclasses import dataclass, field
from typing import Any

from django.template.loader import render_to_string
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.tables import Below, Cell, DataTable, Group, Piece, Row, column_kind
from hq.platform.core.held_values import format_html
from hq.platform.core.templatetags.list_tags import LINE_LEAD
from hq.platform.core.templatetags.table_tags import LINE_FRAME, data_attributes, table_heading
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
    # Saying nothing, it draws nothing, not the empty value's mark: a blank
    # cell, one in the foot beside a total, or one in a column of controls.
    quiet: bool = False
    # Each value on its lines is whole (`cell-part`), so the state they say
    # wraps only between them.
    whole_parts: bool = False


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
    # The blocks its cells keep for its open line, whole and then each under its label.
    blocks: list[Below] = field(default_factory=list)
    entries: list[Below] = field(default_factory=list)
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
    footer = _row(table, table.footer, context, width) if table.footer else None
    if footer:
        footer.facts.section = "tfoot"
    facts = [RowFacts("thead", cells=[facts for _, facts in headings])]
    facts += [row.facts for group in groups for row in group.rows]
    facts += [footer.facts] if footer else []
    laid = lay_out(facts, sorts=table.sorted_by is None and not table.keeps_order and not table.region["chart"])
    # A row across the table stands in the gutter its names keep, holding no toggle.
    gutter = any("row-gutter" in facts.added for _, facts in headings)
    for group in groups:
        for row in group.rows:
            if row.row.across and gutter:
                row.cells[0].facts.added.append("row-gutter")
            _mark(row, width)
    if footer:
        _mark(footer, width)
    sorting = {"table": {"selected_sort": table.sorted_by or ""}}
    return {
        "table": table,
        "headings": [_heading(sorting, column, facts, laid) for column, facts in headings],
        "groups": groups,
        "footer": footer,
        "pieces": _pieces(table, context),
        "marks": mark_safe("".join(laid.marks)) + data_attributes(table.data),  # noqa: S308 - the layout's own attributes
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
                    "hidden": column.hidden or not column.label,
                    "sorts": column.sorts,
                    "title": column.title,
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
    sorts = laid.sorts and column.get("sorts", True)
    said = {name: value for name, value in column.items() if name != "sorts"}
    drawn = table_heading(sorting, **{**said, "css": css}, local_sort=None if sorts else False)
    return {**drawn, "marks": mark_safe("".join(facts.marks))}  # noqa: S308 - the layout's own attributes


def _row(table: DataTable, row: Row, context: Any, width: int) -> DrawnRow:
    cells = [_cell(cell, context) for cell in row.cells]
    footer = row is table.footer
    for drawn_cell, column in zip(cells, table.columns, strict=False):
        drawn_cell.facts.classes = tuple(name for name in column.css.split() if name in _CELL_CLASSES)
        drawn_cell.quiet |= footer or column_kind(column.kind, column.css) == "control"
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
        classes=("row-actions",), says_nothing=not row.controls, controls=row.controls, presses=row.controls
    )


def _mark(row: DrawnRow, width: int) -> None:
    """Write the marks the layout gave the row and its cells."""

    for cell in (row.select, *row.cells, row.actions):
        if cell is None:
            continue
        if row.row.across:
            cell.facts.marks.insert(0, f' colspan="{width}"')
        # The tick-box's and the actions' partials write their own class.
        own = "select-column" if cell is row.select else "row-actions" if cell is row.actions else ""
        cell.attributes = _attributes(cell.facts, cell.role, own, cell.cell.sort)
        cell.box, cell.toggle, cell.named = cell.facts.box, cell.facts.toggle, cell.facts.named
        cell.whole_parts = cell.box == "cell-state" and any(each.parts for each in (cell.cell, *cell.cell.lines))
    attributes = [*row.facts.marks]
    if row.row.id:
        attributes.append(format_html(' id="{}"', row.row.id))
    # Each name is held to an attribute's (``Row``), so it is written as it is.
    attributes.append(data_attributes(row.row.data))
    if row.row.folds:
        attributes.append(format_html(' id="{}" data-folds', row.row.folds))
    if row.row.folded:
        attributes.append(format_html(' data-folded="{}"', row.row.folded))
    if row.row.emphasized:
        attributes.append(" data-emphasized")
    if row.row.attention:
        attributes.append(" data-attention")
    if classes := [*(["tone-attention"] if row.row.attention else []), *row.facts.added]:
        attributes.insert(0, format_html(' class="{}"', " ".join(classes)))
    row.attributes = mark_safe("".join(attributes))  # noqa: S308 - each piece escaped above or the layout's own
    if row.facts.detail:
        row.detail, blocks, entries = row.facts.detail
        row.blocks = [moved.content for moved in blocks if isinstance(moved.content, Below)]
        row.entries = [moved.content for moved in entries if isinstance(moved.content, Below)]


def _attributes(facts: CellFacts, role: str, own: str = "", sort: str = "") -> SafeString:
    classes = " ".join(name for name in (*facts.classes, *facts.added) if name != own)
    return mark_safe(  # noqa: S308 - each value escaped, the rest the layout's own attributes
        (' scope="row"' if facts.row_header else "")
        + (f' class="{escape(classes)}"' if classes else "")
        + (f' data-cell="{escape(role)}"' if role else "")
        + (f' data-sort="{escape(sort)}"' if sort else "")
        + "".join(facts.marks)
    )


def _cell(cell: Cell, context: Any) -> DrawnCell:
    """A cell's facts, read off its declaration."""

    editor = None
    if cell.editor is not None:
        asked = cell.editor
        editor = row_editor(
            context, asked.form, asked.key, label=asked.label, action=asked.action, verb=asked.verb, **asked.initial
        )
    # A run's parts past its lead ask its column for nothing, and it wraps
    # between its parts, so its line is its longest part (``line_list``).
    parts = [part for part in cell.parts[: LINE_LEAD if cell.fit else None] if part.shows not in _OFF_LINE]
    runs = cell.fit and len(cell.parts) > LINE_LEAD
    lengths = [_length(part, after=True) for part in parts]
    on_line = [_length(cell), *([max(lengths, default=0)] if cell.fit else lengths)]
    line = sum(on_line) + (len(str(cell.count)) if cell.count is not None else 0) + cell.fold
    lines = [
        _length(each) + sum(_length(part, after=True) for part in each.parts if part.shows not in _OFF_LINE)
        for each in cell.lines
        if each.shows not in _OFF_LINE
    ]
    fields = cell.field is not None or bool(cell.piece and cell.piece.field)
    tops = [bool(cell.tone or (cell.status and cell.status.pill))] + [False] * (
        (bool(cell.parts) if cell.fit else len(cell.parts)) + bool(cell.lines) + (cell.count is not None) + cell.fold
    )
    facts = CellFacts(
        says_nothing=cell.says_nothing,
        longest=max([line, *lines]),
        controls=bool(cell.url or (cell.link and cell.link.url) or cell.fold or fields or editor),
        fields=fields,
        moved=[Moved("editor", None, editor)]
        if editor and editor["open"]
        else [] + [Moved("below", block.label or None, block) for block in cell.below],
        holds={SHOWN[part.shows] for part in (cell, *cell.parts, *cell.lines) if part.shows in ("more", "narrow-more")}
        | ({"line-rest"} if runs else set()),
        names=int(cell.name),
        row_header=cell.header,
        parted=any(part.role for part in (*cell.parts, *cell.lines)),
        toggled=cell.fold,
        folds=cell.fold,
        tops=tops,
        said=[not cell.says_nothing] + [True] * (len(tops) - 1),
        loose=False,
    )
    return DrawnCell(cell, facts, role=cell.role, editor=editor, quiet=cell.blank)


def _length(cell: Cell, *, after: bool = False) -> int:
    """The characters a value shows on its line, spaces collapsed, and the
    space before it that a part ``after`` another takes where it has no dot."""

    if cell.status is not None:
        words = cell.status.words
    elif cell.piece is not None:
        words = cell.piece.words
    elif cell.moment is not None:
        words = when_text(cell.moment, cell.form if cell.form != "mark" else "")
    elif cell.amount is not None:
        words = str(money(cell.amount))
    else:
        words = (cell.link.label if cell.link else cell.text) + cell.after
    shown = 0 if SAYS_NOTHING.fullmatch(words) else len(" ".join(words.split()))
    return shown + (_DOT if cell.dot else int(after))


def _pieces(table: DataTable, context: Any) -> dict[Piece, SafeString]:
    """Each piece a cell of the table draws, drawn by its part's partial. The
    space a partial's file keeps around its markup is cut: a value starts and
    ends at its words, so none stands after the dot that opens a run's part."""

    rows = [row for group in table.bodies for row in group.rows] + ([table.footer] if table.footer else [])
    request = context.get("request") if hasattr(context, "get") else None
    pieces = {value.piece for row in rows for cell in row.cells for value in _values(cell) if value.piece is not None}
    drawn = {piece: render_to_string(piece.part.partial, dict(piece.context), request) for piece in pieces}
    return {piece: mark_safe(markup.strip()) for piece, markup in drawn.items()}  # noqa: S308 - escaped by its own template


def _values(cell: Cell) -> tuple[Cell, ...]:
    """The cell, and each value it holds on its line, beneath it and below it."""

    lines = (*cell.lines, *(line for block in cell.below for line in block.lines))
    return (cell, *cell.parts, *lines, *(part for line in lines for part in line.parts))
