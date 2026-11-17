"""The tags a data table is declared through.

``{% table_region %}`` wraps the table in the box it fits and draws what
follows from its headings: a figure's alignment down its column, the sort
control only where there are rows to sort, and each row's toggle in the
gutter. ``{% table_heading %}`` draws each column heading with its kind, its
drop rank and its sort control, and ``{% row_toggle %}`` draws the disclosure
of a row in a table with no headings to say where its gutter is.
``{% row_select %}`` draws a row's tick-box under the heading that selects
every row, and ``{% row_state %}`` says how a row stands among the others. Which columns
show is the browser's to say (docs/Design.md, A table fits its box).
"""

import inspect
import re
from typing import Annotated, Any

from django import template
from django.template.base import token_kwargs
from django.template.loader import render_to_string
from django.utils.html import escape, format_html
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import slugify

from hq.platform.application.describe import Describe
from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tables import TableColumn, check_drop, column_kind, ranked
from hq.platform.core.table_cells import (
    Cells,
    LineEntry,
    Point,
    Position,
    Span,
)
from hq.platform.core.template_parts import Part, slotted
lazy from hq.platform.application import history

register = template.Library()

_TAG_NAME = re.compile(r"<\w+")
# The box of the region's own each kind of cell's content is held in (`Cells.boxes`).
_BOXES = {
    "cell-clamp": '<div class="cell-clamp">',
    "cell-short": '<div class="cell-short">',
    "cell-controls": '<div class="cell-controls">',
}
# The order a mark lists its roles in.
_ROLES = ("name", "prose", "data")
_END_TAG = re.compile(r"</\s*\w+\s*>")
_TAGS = re.compile(r"<[^>]*>")


class _Markup:
    """The region's markup by the parser's positions, which count lines by
    newline alone."""

    def __init__(self, markup: str) -> None:
        self.markup = markup
        self.lines = [0] + [found.end() for found in re.finditer("\n", markup)]
        # What is written into the markup at an offset wherever it is shown,
        # on the row and in a copy on its open line, and what is taken out of
        # it everywhere but where it is moved to.
        self.marks: list[tuple[int, str]] = []
        self.cuts: list[tuple[int, int]] = []

    def at(self, position: Position) -> int:
        row, column = position
        return self.lines[row - 1] + column

    def point(self, point: Point) -> int:
        position, past = point
        return self.after(position) if past is None else self.at(position) + past

    def after(self, end_tag: Position) -> int:
        """The offset past the end tag at ``end_tag``."""

        at = self.at(end_tag)
        found = _END_TAG.match(self.markup, at)
        return found.end() if found else at

    def content(self, span: Span, *, whole: bool = False) -> str:
        """The markup from ``span``'s start to its end tag, which a ``whole``
        element includes."""

        return self.marked(self.at(span[0]), self.after(span[1]) if whole else self.at(span[1]))

    def marked(self, start: int, end: int) -> str:
        """The markup from ``start`` to ``end``, with the marks written inside
        it and what was moved from inside it taken out."""

        marks: list[tuple[int, int, Any]] = [(offset, 0, mark) for offset, mark in self.marks if start <= offset < end]
        moved = [(a, 1, b) for a, b in self.cuts if start <= a and b <= end and (a, b) != (start, end)]
        parts, last = [], start
        for offset, moves, value in sorted(marks + moved):
            if offset < last:
                continue
            parts.append(self.markup[last:offset])
            if moves:
                last = value
            else:
                parts.append(value)
                last = offset
        parts.append(self.markup[last:end])
        return "".join(parts)


def _detail_row(markup: _Markup, width: int, blocks: list[Span], entries: list[LineEntry]) -> str:
    """A row's open line, drawn after it and shown while it is open: a cell
    the table's width holding the blocks the row moves there, then each
    labeled block under its label."""

    drawn = "".join(markup.content(block, whole=True) for block in blocks)
    return _open_line(width, drawn, [(entry.name, markup.content(entry.span, whole=True)) for entry in entries])


def _open_line(width: int, held: str, listed: list[tuple[str, str]]) -> str:
    """A row's open line: ``held`` first, then each listed entry's value
    under its label."""

    entries = [f"<div><dt>{escape(name)}</dt><dd>{value}</dd></div>" for name, value in listed]
    columns = f'<dl class="row-columns">{"".join(entries)}</dl>' if entries else ""
    return f'<tr class="row-detail" data-row-detail><td colspan="{width}">{held}{columns}</td></tr>'


# The open line a region whose rows hold cells back draws once, which
# tables.js clones for a row as it opens, an entry for each cell it holds back
# and a box for what the row's line already shows of one.
_LINE_FRAME = f"<template data-open-line>{_open_line(1, '', [('', '<span data-row-shows></span>')])}</template>"


@register.simple_block_tag
def open_line(content):
    """``{% open_line %}value{% endopen_line %}``: an open line holding one
    unlabeled entry, the frame a script clones for a row that comes to hold
    something beneath it once drawn (ask.js), setting the label and width."""

    return mark_safe(_open_line(1, "", [("", content)]))  # noqa: S308 - the body is rendered template output, escaped there


def _unnamed(roles: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(offset, " ".join(role for role in each.split() if role != "name")) for offset, each in roles]


def mark_cells(markup: str, *, sorts: bool = True) -> str | None:
    """The region's tables with each cell marked for what the stylesheet reads.

    A blank body cell is ``data-blank``, a cell of a column blank in every
    row ``data-blank-column``; a cell says the width its column leaves at
    (``data-drop``), a body cell its column's kind, a long one held to a
    few lines (``cell-clamp``), a short one held whole up to its column's
    share (``cell-short``), each in a box of the region's own only where no
    element of the cell can be it (``Cells._box``), and a group's heading
    row spans every declared column. After each row that
    holds something back is its open line (``_detail_row``).
    What a part of a cell is, which decides how it wraps, is its ``data-cell``:
    ``name`` for what names the row, else the role its class declares
    (``table_cells.ROLE_CLASSES``), so the stylesheet never reads a cell's content.
    Without ``sorts`` no row sorts on the page, so no heading keeps its control.
    A region whose rows hold a cell back draws the frame tables.js copies it
    onto the row's open line with (``_LINE_FRAME``), once.
    None when the region holds tables and none of them a row beneath its head.
    """

    cells = Cells(sorts=sorts)
    cells.feed(markup)
    cells.close()
    if cells.bodies and not any(cells.bodies):
        return None
    if not any((cells.marks, cells.details, cells.roles, cells.cuts, cells.classed, cells.toggles, cells.boxes)):
        return markup
    text = _Markup(markup)
    cuts = sorted((text.point(start), text.point(end)) for start, end in cells.cuts)
    text.cuts = cuts
    parts, last = [], 0
    # Nothing goes into what is taken out: a mark inside the editor a row's
    # line takes is written into its copy there.
    inserts = [each for each in _inserts(cells, markup, text) if not any(a < each[0] < b for a, b in cuts)]
    for offset, insert in sorted(inserts, key=lambda each: each[0]):
        parts += [_cut(markup, last, offset, cuts), insert]
        last = offset
    parts.append(_cut(markup, last, len(markup), cuts))
    if cells.held:
        parts.append(_LINE_FRAME)
    return "".join(parts)


def _inserts(cells: Cells, markup: str, text: _Markup) -> list[tuple[int, str]]:
    """What goes into the markup, at each offset."""

    def after_name(start: Position) -> int:
        # A mark goes straight after its tag's name: `<td`, `<strong`.
        return _TAG_NAME.match(markup, text.at(start)).end()

    inserts = [(after_name(start), "".join(marks)) for start, marks in cells.marks.items()]
    roles = [
        (after_name(start), " ".join(role for role in _ROLES if role in each)) for start, each in cells.roles.items()
    ]
    inserts += [(offset, f' data-cell="{role}"') for offset, role in roles]
    # A copy on the open line keeps what each part is, but names no row there.
    text.marks = sorted((offset, f' data-cell="{role}"') for offset, role in _unnamed(roles) if role)
    for point, names in cells.classed.items():
        if point[1] is None:
            inserts.append((after_name(point[0]), f' class="{" ".join(names)}"'))
        else:
            inserts.append((text.point(point), "".join(f"{name} " for name in names)))
    for mark, spans in cells.boxes.items():
        inserts += [
            each for opened, closed in spans for each in ((text.at(opened), _BOXES[mark]), (text.at(closed), "</div>"))
        ]
    if cells.toggles:
        # A row drawn open (its editor open) says so on its toggle.
        toggles = {shown: render_to_string("partials/_row_toggle.html", {"open": shown}) for shown in (False, True)}
        inserts += [(text.at(closed), toggles[shown]) for closed, shown in cells.toggles]
    for end, width, blocks, entries in cells.details:
        inserts.append((text.after(end), _detail_row(text, width, blocks, entries)))
    return inserts


def _cut(markup: str, start: int, end: int, cuts: list[tuple[int, int]]) -> str:
    """The markup from ``start`` to ``end`` with each span in ``cuts`` taken out."""

    parts, last = [], start
    for cut_start, cut_end in cuts:
        if cut_end <= last or cut_start >= end:
            continue
        parts.append(markup[last : max(last, cut_start)])
        last = min(end, cut_end)
    parts.append(markup[last:end])
    return "".join(parts)


def _region(
    label: Annotated[str, Describe("What the table holds, the name its box is a stop by.")],
    *,
    css: Annotated[str, Describe("The classes of where it stands.")] = "",
    chart: Annotated[bool, Describe("A chart's table, read whole in the chart's order and scrolled.")] = False,
    pane: Annotated[bool, Describe("A box of its own height, which scrolls.")] = False,
    compare: Annotated[bool, Describe("Two values side by side in each row, after the row's name.")] = False,
    empty: Annotated[str, Describe("With no rows, what is missing.")] = "",
    remedy: Annotated[Any, Describe("With no rows, what would fill it: a `label` and a `url`.")] = None,
    action_url: Annotated[str, Describe("With no rows, where the link that fills it leads.")] = "",
    action_label: Annotated[str, Describe("That link's label.")] = "",
    filtered: Annotated[bool, Describe("Its own search narrows it, so no rows says nothing matches.")] = False,
    clear: Annotated[str, Describe("The way back from that search.")] = "",
) -> dict[str, Any]:
    """The region a table sits in: the box it fits, named by ``label``.

    A table fits its region by letting ranked columns go (docs/Design.md, A
    table fits its box). A
    ``compare`` table sets two values side by side in each row, after the
    row's name. Only a ``pane`` (a box of its own height) and a ``chart``'s
    table scroll in it, and each is a stop for the keyboard. A chart's table
    is read whole in the chart's own order, so no heading sorts it.

    Tables with no row are drawn as the one empty state instead
    (docs/Design.md, Nothing empty takes space): ``empty`` says what is
    missing, with what would fill it as ``remedy`` (a ``label`` and a
    ``url``) or ``action_url`` and ``action_label``. A list its own
    ``filtered`` search emptied says so instead, with the way back at
    ``clear``.
    """

    classes = [
        *([css] if css else []),
        *(["is-pane"] if pane else []),
        *(["is-compare"] if compare else []),
    ]
    if filtered:
        nothing = {"message": "Nothing matches.", "action_url": clear, "action_label": "Clear filters"}
    else:
        nothing = {
            "message": empty or f"{label}: none yet.",
            "remedy": remedy,
            "action_url": action_url,
            "action_label": action_label,
        }
    return {
        "label": label,
        "css": " ".join(classes),
        "chart": chart,
        "scrolls": pane or chart,
        "empty": nothing,
    }


class RegionNode(template.Node):
    """``{% table_region %}``: what it encloses, in its region."""

    child_nodelists = ("nodelist",)

    def __init__(self, nodelist: Any, label: Any, options: dict[str, Any]) -> None:
        self.nodelist = nodelist
        self.label = label
        self.options = options

    def render(self, context: Any) -> str:
        label = self.label.resolve(context)
        # What would fill an empty table (`page.remedy` reads the database) is
        # asked only of a table with no rows, which is the only one to show it.
        options = {name: value.resolve(context) for name, value in self.options.items() if name != "remedy"}
        region = _region(label, **options)
        content = mark_cells(self.nodelist.render(context), sorts=not region["chart"])
        if content is None:
            # A table with no rows would draw its headings over nothing, so the
            # region says it has nothing instead (docs/Design.md, Nothing empty
            # takes space).
            if "remedy" in self.options:
                region = _region(label, **options, remedy=self.options["remedy"].resolve(context))
            return render_to_string("partials/_empty_state.html", region["empty"])
        # A box that scrolls is a stop for the keyboard, named so the stop says
        # what it holds. It is a group and no landmark: a landmark around it
        # (its section, its card) already has its place in the page's outline,
        # often by the same name. A region that never scrolls is no stop.
        # A table whose rows open an editor ({% row_editor %}) is a region of
        # the page's own (docs/Design.md, A read is a fragment), found by its
        # id in the page that answers it, so an editor opens, saves and closes
        # in place.
        edits = "data-row-edit" in content
        return format_html(
            '<div class="table-scroll{}"{}{}{}>{}</div>',
            f" {region['css']}" if region["css"] else "",
            format_html(' role="group" aria-label="{}" tabindex="0"', region["label"]) if region["scrolls"] else "",
            mark_safe(" data-chart") if region["chart"] else "",
            format_html(' id="table-{}" data-fragment=""', slugify(region["label"])) if edits else "",
            # The region's own content, rendered and escaped already; the mark
            # adds an attribute and nothing else.
            mark_safe(content),  # noqa: S308 - rendered and escaped already
        )


@register.tag("table_region")
def table_region(parser: Any, token: Any) -> RegionNode:
    bits = token.split_contents()
    options = token_kwargs(bits[2:], parser)
    if len(bits) < 2 or len(options) != len(bits) - 2:
        raise template.TemplateSyntaxError(f"table_region takes {inspect.signature(_region)}: a label, then name=value")
    try:
        inspect.signature(_region).bind(bits[1], **options)
    except TypeError as wrong:
        raise template.TemplateSyntaxError(f"table_region takes {inspect.signature(_region)}: {wrong}") from wrong
    nodelist = parser.parse(("endtable_region",))
    parser.delete_first_token()
    return RegionNode(nodelist, parser.compile_filter(bits[1]), options)


# The template contract reads a tag's declaration from what it wraps.
table_region.__wrapped__ = Part(  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    name="table_region",
    about=(
        "The box a table fits, which draws what follows from its headings, and, with no rows, the one empty "
        "state in its place."
    ),
    function=_region,
    public=True,
    example=(
        '{% table_region "Hosts" empty="No hosts yet." %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}</tr></thead><tbody><tr><td>example-host</td></tr></tbody></table>'
        "{% endtable_region %}"
    ),
)


def _server_sort(context: Any, ascending: str, descending: str) -> dict[str, str]:
    """A list's own sort for this column: the order its link asks for and which way it stands."""

    current = context["table"]["selected_sort"]
    descending = descending or f"-{ascending}"
    next_sort = descending if current == ascending else ascending
    return {
        "next": next_sort,
        "direction": "ascending" if current == ascending else "descending" if current == descending else "none",
        "next_direction": "descending" if next_sort == descending else "ascending",
    }


def table_heading(
    context: Any,
    label: Annotated[str, Describe("The column's name.")],
    kind: Annotated[str, Describe("What the column holds, one of `COLUMN_KINDS`; left out, its classes say.")] = "",
    *,
    css: Annotated[str, Describe("The column's classes.")] = "",
    drop: Annotated[int, Describe("The rank it leaves by when the table is too wide, 1 first.")] = 0,
    beneath: Annotated[
        bool, Describe("In a narrow box its cells stand beneath the row's words, under its name.")
    ] = False,
    sort: Annotated[str, Describe("The field a server-sorted list orders this column by.")] = "",
    descending: Annotated[str, Describe("Its reverse, when that is not `-field`.")] = "",
    local_sort: Annotated[
        bool | None, Describe("Sorts the rows on the page; left out, it follows from what the column holds.")
    ] = None,
    hidden: Annotated[bool, Describe("The name for assistive technology only.")] = False,
    title: Annotated[str, Describe("Its `title`.")] = "",
    gutter: Annotated[bool, Describe("Its cells stand in the gutter and hold each row's toggle.")] = False,
    select_all: Annotated[bool, Describe("The tick-box that selects every row.")] = False,
) -> dict[str, Any]:
    """One column heading.

    ``drop`` is the order a table too wide for its region lets the column go
    in, 1 first, the rightmost of a rank first. ``beneath`` keeps a column of
    words every row must show (a change's two sides): in a narrow box each of
    its cells stands on a line beneath the row's words, under the column's
    name, as a row's controls do. ``sort`` names the field a
    server-sorted list orders this column by (``descending`` when its reverse is
    not ``-field``). Any other column that says something sorts the rows
    already on the page, whose region keeps the control only where it holds
    two rows to sort; a column of controls, or one named for assistive
    technology alone, has nothing to put in order. ``local_sort=False`` keeps
    a table's own order, such as a page of a list its server orders.
    """

    if sort and local_sort:
        raise ValueError(f"Column {label!r} sorts on the server or on the page, not both.")
    kind = "control" if select_all else column_kind(kind, css)
    if local_sort is None:
        local_sort = not sort and kind != "control" and not hidden and bool(label)
    check_drop(drop, css, label)
    if beneath and drop:
        raise ValueError(f"Column {label!r} stands beneath its row or leaves by its rank, not both.")
    classes = [css] if css else []
    classes += ["select-column"] if select_all else []
    classes += ["row-gutter"] if gutter else []
    return {
        "label": label,
        "kind": kind,
        "css": " ".join(classes),
        "drop": drop,
        "beneath": beneath,
        "server_sort": _server_sort(context, sort, descending) if sort else None,
        "local_sort": local_sort,
        "hidden": hidden,
        "title": title,
        "select_all": select_all,
    }


def row_group(
    label: Annotated[str, Describe("The group's name.")],
    count: Annotated[int | None, Describe("How many rows it holds.")] = None,
    *,
    folds: Annotated[str, Describe("The row's id, for a group that starts folded away.")] = "",
    href: Annotated[str, Describe("The page its rows are dealt with on together, which its name links to.")] = "",
    attention: Annotated[bool, Describe("A group a reader should weigh before the rest.")] = False,
) -> dict[str, Any]:
    """The heading row of one group of a table's rows, first in that group's
    ``tbody``: its name and, given one, how many rows it holds. It spans every
    column, which its region counts from the table's headings, and its name
    keeps the edge of the rows' names. ``folds`` is the row's id when the group
    starts folded away: its rows name it in ``data-folded``, and its toggle
    stands in the rows' gutter. ``href`` is the page the group's rows are
    dealt with on together, which its name links to. ``attention`` marks a
    group a reader should weigh before the rest, such as a change that lets a
    record do more, whose words beside its name say why."""

    return {"label": label, "count": count, "folds": folds, "href": href, "attention": attention}


@register.inclusion_tag("partials/_history_table.html", takes_context=True)
def history_table(context: Any, label: str, groups: Any, *, hide_who: bool = False) -> dict[str, Any]:
    """A table of audit events, each drawn by the row the audit log draws
    (``partials/_history_row.html``): ``groups`` is a sequence of
    ``history.HistoryGroup``. ``hide_who`` leaves out who did each, for a
    reader who may not read the audit log."""

    columns = ranked(
        tuple(
            TableColumn(heading, css=css)
            for heading, css in history.COLUMNS
            if not (hide_who and heading == history.WHO)
        )
    )
    return {
        "request": context.get("request"),
        "label": label,
        "groups": groups,
        "columns": columns,
        "hide_who": hide_who,
    }


@register.simple_tag
def history_row_attributes(row: Any, folds: str = "") -> SafeString:
    """The ``tr`` attributes of a line of history (``_history_row.html``): a
    row in a group folded away (``folds``), or under its event, is folded
    under it; an event that changed things holds the fold."""

    if folds or row.under:
        return html_attributes({"data-folded": folds or row.under})
    return html_attributes({"id": row.fold, "data-folds": True}) if row.grouped else SafeString("")


def row_actions(
    actions: Annotated[Any, Describe("What can be done to the row's record, each a `PageAction`.")] = (),
    *,
    gap: Annotated[Any, Describe("A `WriteGap` standing in for every action the connection cannot make.")] = None,
    first: Annotated[int, Describe("How many show; the rest wait in the row's open line.")] = 0,
    put_away: Annotated[Any, Describe("The reader's own way to set the row aside, last of its buttons.")] = None,
    mark: Annotated[Any, Describe("A `RowMark` the reader keeps on the row's record, first of its controls.")] = None,
    order: Annotated[Any, Describe("A `RowOrder` that moves the row in an order the reader keeps.")] = None,
) -> dict[str, Any]:
    """The cell that ends a row with what can be done to its record: each of
    ``actions`` (a ``PageAction``, as a ``ListRow``'s are) a compact control
    drawn by ``_page_action.html``, so a row's buttons look like every other
    and only a destructive one differs. Under a heading of kind ``control``
    (``css="actions-col"``), so the region sizes the column to what it holds
    and a narrow box sets it beneath the row's words. ``gap``, an
    ``adoption.WriteGap``, stands in for every action when the connection
    cannot make the change. Given ``first``, the actions past that many wait
    in the row's open line. ``put_away``, a POST ``PageAction`` such as the
    queue's Dismiss, is the reader's own way to set the row aside: the last
    of its buttons, drawn as they are, and never waiting in the open line.
    ``mark`` (a ``RowMark``, such as a favorite's star) and ``order`` (a
    ``RowOrder``) come first, the star last of the two, so a row's own
    arrangement is a column the table sizes and counts, never a part of the
    cell that names the row."""

    actions = tuple(actions or ())
    shown, later = (actions[:first], actions[first:]) if first else (actions, ())
    return {"actions": shown, "later": later, "gap": gap, "put_away": put_away, "mark": mark, "order": order}


def row_toggle() -> dict[str, Any]:
    """The toggle that opens a row to all it holds, last in the cell that names
    the row, in a table with no headings; under a heading that keeps the gutter
    (``gutter=True``) the region draws it. The stylesheet shows it only where
    the row has something hidden."""

    return {}


def row_select(pk: Any) -> dict[str, Any]:
    """The cell that holds a row's tick-box, first in a row under the heading
    that selects every row (``select_all=True``), keyed by ``pk``. A row with
    no key keeps the cell empty, so its columns stay under their headings."""

    return {"pk": pk}


@register.simple_tag
def row_state(*, emphasized: bool = False) -> str:
    """How a row stands among the others, written in its ``tr``:
    ``emphasized`` for one that asks to be read first, such as a message not
    yet read, whose row is tinted and whose links weigh more."""

    return mark_safe(" data-emphasized") if emphasized else ""


# How many of a line list's parts show on its row, wrapping with its column
# as any value's words do; the whole run shows on the row's open line.
LINE_LEAD = 3


class LineListNode(template.Node):
    def __init__(self, nodelist: Any, values: Any, name: str, list_class: Any) -> None:
        self.nodelist = nodelist
        self.values = values
        self.name = name
        self.list_class = list_class

    def _parts(self, context: Any, values: list[Any]) -> str:
        drawn = []
        for value in values:
            with context.push(**{self.name: value}):
                drawn.append(self.nodelist.render(context))
        return "".join(drawn)

    def render(self, context: Any) -> str:
        values = [value for value in self.values.resolve(context) or () if value not in (None, "")]
        if not values:
            return ""
        css = str(self.list_class.resolve(context)) if self.list_class else ""
        lead, rest = values[:LINE_LEAD], values[LINE_LEAD:]
        # Parts already rendered and escaped by their own template.
        parts = mark_safe(self._parts(context, lead))  # noqa: S308 - rendered and escaped already
        tail = mark_safe(self._parts(context, rest))  # noqa: S308 - rendered and escaped already
        # The rest shows only on the row's open line (table_cells._HELD).
        if css:
            more = format_html('<li class="line-rest"><ul class="{} line-run">{}</ul></li>', css, tail) if rest else ""
            return format_html('<ul class="{} line-list">{}{}</ul>', css, parts, more)
        more = format_html('<span class="line-rest"><span class="line-run">{}</span></span>', tail) if rest else ""
        return format_html('<span class="line-list">{}{}</span>', parts, more)


@register.tag("line_list")
def line_list(parser: Any, token: Any) -> LineListNode:
    """``{% line_list row.tech_list as name %}{% status name chip=True %}{% endline_list %}``:
    a run of short parts in a cell, each drawn by the body: the first
    ``LINE_LEAD`` on the row, wrapping with its column, and the whole run on
    the row's open line, which the row offers once it holds more.
    ``list="dot-list"`` draws it as that list, the body drawing each ``li``."""

    bits = token.split_contents()
    if len(bits) not in (4, 5) or bits[2] != "as":
        raise template.TemplateSyntaxError('line_list takes values "as" name, then optionally list="<class>"')
    options = token_kwargs(bits[4:], parser)
    if len(options) != len(bits[4:]) or set(options) - {"list"}:
        raise template.TemplateSyntaxError('line_list takes one option, list="<class>"')
    nodelist = parser.parse(("endline_list",))
    parser.delete_first_token()
    return LineListNode(nodelist, parser.compile_filter(bits[1]), bits[3], options.get("list"))


slotted(
    Part(
        name="table_heading",
        classes=("sort-indicator", "table-sort-link"),
        about="One column heading of a table: its kind, the rank it leaves by and its sort control.",
        partial="partials/_table_heading.html",
        function=table_heading,
        public=True,
        example=(
            '{% table_region "Example updates" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Name" %}{% table_heading "Updated" "status" drop=2 %}</tr></thead>'
            "<tbody><tr><td>example-host</td><td>An hour ago</td></tr></tbody></table>{% endtable_region %}"
        ),
    ),
    library=register,
)
slotted(
    Part(
        name="row_group",
        classes=("row-group",),
        about="The heading row of one group of a table's rows, first in its `tbody`: its name and how many rows.",
        partial="partials/_row_group.html",
        function=row_group,
        public=True,
        example=(
            '{% table_region "Example groups" %}<table class="data-table"><tbody>{% row_group "Example group" 3 %}'
            "<tr><td>example-host</td></tr></tbody></table>{% endtable_region %}"
        ),
    ),
    library=register,
)
slotted(
    Part(
        name="row_actions",
        about="The cell that ends a row with what can be done to its record, each a compact control.",
        partial="partials/_row_actions.html",
        function=row_actions,
        public=True,
        example=(
            '{% table_region "Example actions" %}<table class="data-table"><tbody><tr><td>{{ row.name }}</td>'
            "{% row_actions row.actions %}</tr></tbody></table>{% endtable_region %}"
        ),
        classes=("row-later",),
        public_classes=("row-actions",),
    ),
    library=register,
)
slotted(
    Part(
        name="row_select",
        example=(
            '{% table_region "Example selections" %}<table class="data-table" data-selectable-table><thead><tr>'
            '{% table_heading "Select all rows" select_all=True %}{% table_heading "Name" "identifier" gutter=True %}'
            '{% table_heading "State" "status" %}</tr></thead><tbody><tr>{% row_select 1 %}<td>example-01</td>'
            '<td>{% status "Online" tone="good" %}</td></tr></tbody></table>{% endtable_region %}'
        ),
        about="The cell that holds a row's tick-box, first in a row under the heading that selects every row.",
        partial="partials/_row_select.html",
        function=row_select,
        classes=("select-column",),
    ),
    library=register,
)
slotted(
    Part(
        name="row_toggle",
        classes=("row-expand",),
        about="The toggle that opens a row to all it holds, last in the cell that names it, in a table with no headings.",
        partial="partials/_row_toggle.html",
        function=row_toggle,
        public=True,
        example=(
            '{% table_region "Example toggles" %}<table class="data-table"><tbody><tr>'
            "<td>{{ row.name }}{% row_toggle %}</td></tr></tbody></table>{% endtable_region %}"
        ),
    ),
    library=register,
)
