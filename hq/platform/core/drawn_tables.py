"""A declared table (``tables.DataTable``) as ``partials/_data_table.html``
draws it: each heading and cell's facts read off its declaration, laid out
by ``table_layout.lay_out``, and every mark it gives written where the
template draws the element it belongs to. No markup is read back."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import slugify

from hq.platform.application.asks import IDLE
from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tables import Below, Cell, DataTable, Group, Piece, Row, column_kind
from hq.platform.core.held_values import format_html
from hq.platform.core.template_parts import enclosed
from hq.platform.core.templatetags.frame_tags import muted
from hq.platform.core.templatetags.line_breaks import breakable, code_spans
from hq.platform.core.templatetags.list_tags import LINE_LEAD, LIST, line_run
from hq.platform.core.templatetags.status_tags import status
from hq.platform.core.templatetags.table_tags import LINE_FRAME, data_attributes, table_heading
from hq.platform.core.templatetags.value_tags import (
    cell_line,
    dot,
    empty_value,
    entity,
    money,
    outside,
    readout,
    when,
    when_text,
)
from hq.platform.core.templatetags.write_tags import row_editor

from .table_layout import GIST, SAYS_NOTHING, CellFacts, Moved, RowFacts, TableFacts, lay_out

# The class a value that shows only part of the time is drawn in, which the
# open line reads (``table_layout.HELD``).
SHOWN = {"more": "row-more", "narrow": "narrow-only", "narrow-more": "narrow-more", "gist": GIST}
# The classes of a column its cells carry too: a moment keeps its line (tables.css).
_CELL_CLASSES = frozenset({"date-col"})
# What the row's line never measures of a value: one drawn only once it opens
# or in a narrow box, and a gist, cut to the room its line leaves.
_OFF_LINE = frozenset({"more", "narrow", "gist"})
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
    # The cell whole: its start tag, its value in its box, and its toggle.
    drawn: SafeString = field(default_factory=SafeString)


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
    # The blocks its cells keep for its open line, drawn: whole, then each under its label.
    blocks: list[SafeString] = field(default_factory=list)
    entries: list[tuple[str, SafeString]] = field(default_factory=list)
    # A cell for its tick-box and one for its actions, each with its marks.
    select: DrawnCell | None = None
    actions: DrawnCell | None = None


@dataclass(slots=True)
class DrawnGroup:
    """A group of rows, and how many columns its heading spans."""

    group: Group
    rows: list[DrawnRow]
    across: int


def drawn(table: DataTable, context: Any, *, empty: str = "") -> dict[str, Any]:
    """What ``_data_table.html`` draws of ``table``."""

    headings = _headings(table)
    width = len(headings)
    groups = [
        DrawnGroup(group, [_row(table, row, context, width) for row in group.rows], width) for group in table.bodies
    ]
    footer = _row(table, table.footer, context, width) if table.footer else None
    if footer:
        footer.facts.section = "tfoot"
    facts = [RowFacts("thead", cells=[facts for _, facts in headings])] if table.headed else []
    facts += [row.facts for group in groups for row in group.rows]
    facts += [footer.facts] if footer else []
    laid = lay_out(facts, sorts=table.sorted_by is None and not table.keeps_order and not table.region["chart"])
    # A row across the table stands in the gutter its names keep, holding no toggle.
    gutter = any("row-gutter" in facts.added for _, facts in headings)
    drawing = _Drawing(context)
    drawing.pieces = _pieces(table, drawing)
    for group in groups:
        for row in group.rows:
            if row.row.across and gutter:
                row.cells[0].facts.added.append("row-gutter")
            _mark(row, width, drawing)
    if footer:
        _mark(footer, width, drawing)
    sorting = {"table": {"selected_sort": table.sorted_by or ""}}
    rows = any(group.rows for group in groups)
    return {
        "box": _box(table, groups) if rows else "",
        "empty": "" if rows else _empty(table, empty),
        "table": table,
        "headings": [_heading(sorting, column, facts, laid) for column, facts in headings] if table.headed else [],
        "groups": groups,
        "footer": footer,
        "marks": mark_safe("".join(laid.marks)) + data_attributes(table.data),  # noqa: S308 - the layout's own attributes
        "rows": rows,
        # The open line a table whose rows hold cells back draws once, which tables.js clones.
        "frame": mark_safe(LINE_FRAME) if laid.held else "",  # noqa: S308 - the table's own literal markup
    }


def _box(table: DataTable, groups: list[DrawnGroup]) -> SafeString:
    """The attributes of the box a table fits past its classes (``data_table``'s rules).

    A box that scrolls is a stop for the keyboard, named so the stop says what
    it holds: a group and no landmark, since a landmark around it (its
    section, its card) already has its place in the page's outline. A table
    whose rows open an editor is a region of the page's own, found by its id
    in the page that answers it.
    """

    region = table.region
    edits = any(cell.editor for group in groups for row in group.rows for cell in row.row.cells)
    return format_html(
        "{}{}{}",
        format_html(' role="group" aria-label="{}" tabindex="0"', table.label)
        if region["pane"] or region["chart"]
        else "",
        mark_safe(" data-chart") if region["chart"] else "",
        format_html(' id="table-{}" data-fragment=""', slugify(table.label)) if edits else "",
    )


def _empty(table: DataTable, empty: str) -> dict[str, Any]:
    """What a table with no rows says in their place: that its filter emptied
    it, or what is missing and what would fill it. What would fill it
    (``page.remedy`` reads the database) is asked only here."""

    region = table.region
    if region["filtered"]:
        return {"message": "Nothing matches.", "action_url": region["clear"], "action_label": "Clear filters"}
    remedy = region["remedy"]
    return {
        "message": empty or region["empty"] or f"{table.label}: none yet.",
        "remedy": remedy() if callable(remedy) else remedy,
        "action_url": region["action_url"],
        "action_label": region["action_label"],
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
                    classes=tuple(column.css.split()),
                    kind=column_kind(column.kind, column.css),
                    drop=column.drop,
                    label=label,
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
    footer = row is table.footer
    cells = [_cell(_name(cell) if at == 0 and not footer else cell, context) for at, cell in enumerate(row.cells)]
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
        # An ask shows its state's word alone in the row, so its open line
        # says the word and the sentence, where ask.js keeps them (``_ask.html``).
        drawn_row.actions.facts.moved += [
            Moved(
                "below", ask.label, Below(ask.label, (Cell(f"{said.label}: {said.note}", data={"ask-why": ask.url}),))
            )
            for ask in row.asks
            if (said := ask.standing).state != IDLE and said.note
        ]
    if row.evidence and drawn_row.facts.cells:
        drawn_row.facts.cells[-1].moved.append(Moved("below", "evidence", row.evidence))
    drawn_row.editor = next((cell.editor for cell in cells if cell.editor and cell.editor["open"]), None)
    return drawn_row


def _name(cell: Cell) -> Cell:
    """The cell of the column that names each row (the grid's `name` track)
    as the row's name, where its value is words or a link to its record's
    page: so every table names its rows alike, and no table declares it. A
    row's label, an identifier drawn as code, a sentence, a muted note, a
    moment, an amount or a state names nothing."""

    plain = not (cell.header or cell.readout or cell.muted or cell.tone or cell.quoted or cell.role == "prose")
    return replace(cell, name=True) if plain and (cell.text or cell.link) else cell


def _actions(row: Row) -> CellFacts:
    return CellFacts(
        classes=("row-actions",), says_nothing=not row.controls, controls=row.controls, presses=row.controls
    )


def _mark(row: DrawnRow, width: int, drawing: _Drawing) -> None:
    """Write the marks the layout gave the row and its cells, and draw their values."""

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
    for cell in row.cells:
        cell.drawn = _whole(cell, drawing)
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
        row.blocks = [
            _block(moved.content.lines, None, drawing) for moved in blocks if isinstance(moved.content, Below)
        ]
        row.entries = [
            (moved.content.label, _block(moved.content.lines, None, drawing))
            for moved in entries
            if isinstance(moved.content, Below)
        ]


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
        holds={
            SHOWN[part.shows]
            for part in (cell, *cell.parts, *cell.lines)
            if part.shows in ("more", "narrow-more", "gist")
        }
        | ({"line-rest"} if runs else set()),
        names=int(cell.name),
        row_header=cell.header,
        parted=any(part.role for part in (*cell.parts, *cell.lines)),
        folds=cell.fold,
        tops=tops,
        said=[not cell.says_nothing] + [True] * (len(tops) - 1),
    )
    # A role whose words are one link is that link's, which no rule for a link among words reaches.
    role = "" if _linked(cell) else cell.role
    return DrawnCell(cell, facts, role=role, editor=editor, quiet=cell.blank)


def _linked(value: Cell) -> bool:
    """Whether a value's words are one plain link of HQ's."""

    return bool(value.url) and not (value.link or value.external or value.tone or value.cut or value.name)


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
        words = (cell.link.label if cell.link else cell.text or cell.quoted) + cell.after
    shown = 0 if SAYS_NOTHING.fullmatch(words) else len(" ".join(words.split()))
    return shown + (_DOT if cell.dot else int(after))


def _pieces(table: DataTable, drawing: _Drawing) -> dict[Piece, SafeString]:
    """Each piece a cell of the table draws, drawn by its part's partial as an
    include draws it, never through the page's context processors again. The
    space a partial's file keeps around its markup is cut: a value starts and
    ends at its words, so none stands after the dot that opens a run's part."""

    rows = [row for group in table.bodies for row in group.rows] + ([table.footer] if table.footer else [])
    pieces = {value.piece for row in rows for cell in row.cells for value in _values(cell) if value.piece is not None}
    drawn = {piece: drawing.included(piece.part.partial, **piece.context) for piece in pieces}
    return {piece: mark_safe(markup.strip()) for piece, markup in drawn.items()}  # noqa: S308 - escaped by its own template


def _values(cell: Cell) -> tuple[Cell, ...]:
    """The cell, and each value it holds on its line, beneath it and below it."""

    lines = (*cell.lines, *(line for block in cell.below for line in block.lines))
    return (cell, *cell.parts, *lines, *(part for line in lines for part in line.parts))


@dataclass(slots=True)
class _Drawing:
    """What a cell's markup reads beyond its declaration: the page being
    drawn, which an entity's link to itself reads and a partial is drawn in,
    each piece the table draws, and a row's toggle, drawn once each way."""

    context: Any
    pieces: Mapping[Piece, SafeString] = field(default_factory=dict)
    toggles: dict[bool, SafeString] = field(default_factory=dict)

    def included(self, name: str, **values: Any) -> SafeString:
        """A partial as ``{% include name with ... only %}`` draws it here."""

        return self.context.template.engine.get_template(name).render(self.context.new(values))

    def toggle(self, shown: bool) -> SafeString:
        if shown not in self.toggles:
            self.toggles[shown] = self.included("partials/_row_toggle.html", open=shown)
        return self.toggles[shown]


def _safe(*drawn: str) -> SafeString:
    return mark_safe("".join(drawn))  # noqa: S308 - each piece escaped by what drew it, the rest literal markup


def _whole(cell: DrawnCell, drawing: _Drawing) -> SafeString:
    """A cell whole: its fold's toggle, then its field, its row's way into
    its editor or its value, held in its box, and its row's toggle last."""

    value, editor = cell.cell, cell.editor
    if value.field:
        inside = drawing.included("partials/_row_field.html", field=value.field)
    elif editor:
        said = {name: editor[name] for name in ("open", "slug", "label", "open_url", "close_url")}
        inside = drawing.included("partials/_row_editor.html#way_in", **said)
    else:
        inside = _content(cell, drawing)
    if cell.box == "cell-state":
        inside = _safe("<div>", inside, "</div>")
    if cell.box:
        inside = _safe('<div class="', cell.box, '">', inside, "</div>")
    # A fold's toggle is the cell's own child, outside the value's box, so it
    # stands in the gutter where the row's toggle does (tables.css) and the
    # value's words start where every row's do.
    if value.fold:
        inside = _safe(drawing.included("partials/_row_fold.html", label=value.text), inside)
    toggle = drawing.toggle(cell.toggle) if cell.toggle is not None else ""
    tag = "th" if value.header else "td"
    return _safe("<", tag, cell.attributes, ">", inside, toggle, "</", tag, ">")


def _content(cell: DrawnCell, drawing: _Drawing) -> SafeString:
    """A cell's value, then what follows it on its line, after a space or its
    dot, and its lines beneath."""

    value = cell.cell
    drawn = [_value(value, cell, drawing)]
    if value.fit:
        parts = [_safe("<li>", _value(part, cell, drawing, own=False), "</li>") for part in value.parts]
        drawn.append(line_run(parts, kind="dots"))
    else:
        drawn += [_after(part, cell, drawing) for part in value.parts]
    # A gist takes the room its line leaves (cuts.css), so the line is a box of its own.
    if value.lead or any(each.shows == "gist" for each in (value, *value.parts)):
        drawn = [_safe('<div class="row-line">', *drawn, "</div>")]
    if value.lines:
        drawn.append(_block(value.lines, cell, drawing))
    return _safe(*drawn)


def _block(lines: tuple[Cell, ...], cell: DrawnCell | None, drawing: _Drawing) -> SafeString:
    """Lines read down, each one value and its parts, a muted one muted whole;
    a ``fit`` line is a dots list of its own, each part an item."""

    blocks: list[SafeString] = []
    items = []
    for line in lines:
        if line.says_nothing:
            continue
        if line.fit:
            if items:
                blocks.append(enclosed(LIST, _safe(*items)))
                items = []
            run = _safe(*(_safe("<li>", _value(part, cell, drawing, own=False), "</li>") for part in line.parts))
            blocks.append(enclosed(LIST, run, kind="dots", muted=line.muted))
            continue
        shown = f' class="{SHOWN[line.shows]}"' if line.shows else ""
        drawn = _value(line, cell, drawing, own=False, whole=True, hushed=line.muted)
        drawn = _safe(drawn, *(_after(part, cell, drawing, hushed=line.muted) for part in line.parts))
        items.append(
            _safe("<li", shown, data_attributes(line.data), ">", muted(drawn) if line.muted else drawn, "</li>")
        )
    if items or not blocks:
        blocks.append(enclosed(LIST, _safe(*items)))
    return _safe(*blocks)


def _after(part: Cell, cell: DrawnCell | None, drawing: _Drawing, *, hushed: bool = False) -> SafeString:
    """A part after what stands before it on its line: a space, unless its dot joins it."""

    drawn = _value(part, cell, drawing, own=False, hushed=hushed)
    return drawn if part.dot else _safe(" ", drawn)


def _value(
    value: Cell,
    cell: DrawnCell | None,
    drawing: _Drawing,
    *,
    own: bool = True,
    whole: bool = False,
    hushed: bool = False,
) -> SafeString:
    """One value as its declaration says it: around its words, from the
    inside out, its kind's markup, its bold or link, what a reader hears, its
    tip or muted voice, its box, its dot, its span when it shows only part
    of the time, its count, and its hooks. ``own`` is the cell's own value,
    whose bold names its row; ``whole`` a line, whose own box is its item;
    ``hushed`` a value on a line muted whole."""

    drawn = _named(value, cell, drawing, own=own)
    gist = value.shows == "gist"
    if value.aloud:
        drawn = format_html(
            '<span aria-hidden="true">{}</span><span class="visually-hidden">{}</span>', drawn, value.aloud
        )
    if gist:
        # A gist is the quiet line its row's toggle releases, its hooks on it.
        hooks = {f"data_{name.replace('-', '_')}": said or True for name, said in value.data.items()}
        drawn = muted(drawn, css=SHOWN["gist"], tip=value.tip, title=value.title, **hooks)
    elif value.muted and not hushed:
        drawn = muted(drawn, tip=value.tip, title=value.title)
    elif value.tip or value.title:
        tipped = html_attributes({"title": value.title, "tabindex": "0" if value.tip else None, "data-tip": value.tip})
        drawn = _safe("<span", tipped, ">", drawn, "</span>")
    if cell is not None and cell.whole_parts:
        drawn = _safe('<span class="cell-part">', drawn, "</span>")
    if value.dot:
        drawn = _safe(dot(), drawn)
    if value.shows and not (whole or gist):
        drawn = _safe('<span class="', SHOWN[value.shows], '">', drawn, "</span>")
    if value.count is not None:
        drawn = _safe(drawn, ' <span class="count">', breakable(value.count), "</span>")
    if value.data and not (whole or gist or (value.role and _linked(value))):
        drawn = _safe("<span", data_attributes(value.data), ">", drawn, "</span>")
    return drawn


def _named(value: Cell, cell: DrawnCell | None, drawing: _Drawing, *, own: bool) -> SafeString:
    """A value in bold where it is a name, a link round the bold of a name
    that only links, and in a span its role marks where it is one of a cell's parts."""

    names = ' data-cell="name"' if own and cell is not None and cell.named else ""
    if value.name and value.url and not (value.link or value.external or value.tone or value.cut):
        opened = _safe(format_html('<a href="{}"', value.url), data_attributes(value.data), ">")
        return _safe(opened, "<strong", names, ">", _text(value), "</strong></a>")
    if value.name:
        return _safe("<strong", names, ">", _words(value, cell, drawing), "</strong>")
    if value.role and _linked(value):
        opened = format_html('<a data-cell="{}" href="{}"', value.role, value.url)
        return _safe(opened, data_attributes(value.data), ">", _text(value), "</a>")
    if value.role and not own:
        return _safe(format_html('<span data-cell="{}">', value.role), _words(value, cell, drawing), "</span>")
    return _words(value, cell, drawing)


def _words(value: Cell, cell: DrawnCell | None, drawing: _Drawing) -> SafeString:
    """A value's words as what it is draws them: a state, a piece, an entity,
    a link, a cut line, a moment, an amount, text, or the empty value's mark."""

    context = drawing.context
    if value.status is not None:
        return breakable(value.status)
    if value.piece is not None:
        return drawing.pieces.get(value.piece, SafeString())
    if value.tone and value.link:
        return _safe(entity(context, value.link, code=value.readout), " ", status(value.text, tone=value.tone))
    if value.tone:
        return status(value.text, tone=value.tone, href=value.url)
    if value.link:
        return entity(context, value.link, code=value.readout)
    if value.url and value.external:
        return entity(context, outside(value.url), label=value.text)
    if value.cut:
        return cell_line(value.text, value.url)
    if value.url:
        return _safe(format_html('<a href="{}">', value.url), _text(value), "</a>")
    if value.moment is not None:
        return when(value.moment, value.form)
    if value.amount is not None:
        return breakable(money(value.amount))
    if value.text or value.quoted:
        return _text(value)
    if cell is not None and cell.cell.says_nothing and not cell.quiet:
        return empty_value()
    return SafeString()


def _text(value: Cell) -> SafeString:
    said = code_spans(value.quoted) if value.quoted else readout(value.text) if value.readout else breakable(value.text)
    return _safe(said, breakable(value.after))
