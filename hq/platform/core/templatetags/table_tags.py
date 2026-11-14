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

import html
import inspect
import re
from functools import wraps
from typing import Any

from django import template
from django.template.base import token_kwargs
from django.template.loader import render_to_string
from django.utils.html import escape, format_html
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import slugify

from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tables import TableColumn, check_drop, column_kind, ranked
from hq.platform.core.table_cells import (
    SAYS_NOTHING,
    Cells,
    LineEntry,
    Point,
    Position,
    Span,
)
lazy from hq.platform.application import history

register = template.Library()

_TAG_NAME = re.compile(r"<\w+")
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
        # on the row and in a copy on its open line.
        self.marks: list[tuple[int, str]] = []

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

    def content(self, span: Span, cut: list[Span] = [], *, whole: bool = False) -> str:  # noqa: B006 (never written)
        """The markup from ``span``'s start to its end tag, which a ``whole``
        element includes, with the elements in ``cut`` taken out."""

        parts, last = [], self.at(span[0])
        for start, end in sorted(cut, key=lambda each: self.at(each[0])):
            parts.append(self.marked(last, self.at(start)))
            last = self.after(end)
        parts.append(self.marked(last, self.after(span[1]) if whole else self.at(span[1])))
        return "".join(parts)

    def marked(self, start: int, end: int) -> str:
        """The markup from ``start`` to ``end``, with the marks written inside it."""

        parts, last = [], start
        for offset, mark in self.marks:
            if start <= offset < end:
                parts += [self.markup[last:offset], mark]
                last = offset
        parts.append(self.markup[last:end])
        return "".join(parts)


def _says_something(markup: str) -> bool:
    return not SAYS_NOTHING.fullmatch(html.unescape(_TAGS.sub("", markup)))


def _detail_row(markup: _Markup, width: int, blocks: list[Span], entries: list[LineEntry]) -> str:
    """A row's open line, drawn after it and shown while it is open: a cell
    the table's width holding the blocks the row keeps beneath itself, then
    the parts it holds back, each named by its column. A column's part shows
    while the column has left (``data-dropped``, tables.js), a narrow part in
    a narrow box, a labeled block always."""

    drawn = [markup.content(block, whole=True) for block in blocks]
    listed = []
    for entry in entries:
        shown = "".join(markup.content(span, entry.cut, whole=entry.whole) for span in entry.spans)
        if entry.how == "column" and not _says_something(shown):
            continue
        how = f' data-column="{entry.column}"' if entry.how == "column" else f" data-{entry.how}"
        term = ' class="visually-hidden"' if entry.unseen else ""
        css = f' class="{" ".join(entry.css)}"' if entry.css else ""
        css += ' data-cell="data"' if entry.data else ""
        listed.append(f"<dt{how}{term}>{escape(entry.name)}</dt><dd{how}{css}>{shown}</dd>")
    if not drawn and not listed:
        return ""
    columns = f'<dl class="row-columns">{"".join(listed)}</dl>' if listed else ""
    return f'<tr class="row-detail" data-row-detail><td colspan="{width}">{"".join(drawn)}{columns}</td></tr>'


def _unnamed(roles: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(offset, " ".join(role for role in each.split() if role != "name")) for offset, each in roles]


def mark_cells(markup: str, *, sorts: bool = True) -> str | None:
    """The region's tables with each cell marked for what the stylesheet reads.

    A blank body cell is ``data-blank``, a cell of a column blank in every row
    ``data-blank-column``, prose or a status past its floor ``data-long``, a
    status said by one pill alone ``data-pill-only``, and a group's heading row
    spans every declared column. What a kept line says is
    one ``.kept-line``, and after each row that holds something back is its open
    line (``_detail_row``), so opening a row moves nothing of the row itself.
    What a part of a cell is, which decides how it wraps, is its ``data-cell``:
    ``name`` for what names the row, else the role its class declares
    (``table_cells.ROLE_CLASSES``), so the stylesheet never reads a cell's content.
    Without ``sorts`` no row sorts on the page, so no heading keeps its control.
    None when the region holds tables and none of them a row beneath its head.
    """

    cells = Cells(sorts=sorts)
    cells.feed(markup)
    cells.close()
    if cells.bodies and not any(cells.bodies):
        return None
    if not any((cells.marks, cells.held, cells.details, cells.roles, cells.cuts, cells.gutters, cells.toggles)):
        return markup
    text = _Markup(markup)
    cuts = sorted((text.point(start), text.point(end)) for start, end in cells.cuts)
    parts, last = [], 0
    # Nothing goes into what is taken out: a mark inside the editor a row's
    # line takes is written into its copy there.
    inserts = [each for each in _inserts(cells, markup, text) if not any(a < each[0] < b for a, b in cuts)]
    for offset, insert in sorted(inserts, key=lambda each: each[0]):
        parts += [_cut(markup, last, offset, cuts), insert]
        last = offset
    parts.append(_cut(markup, last, len(markup), cuts))
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
    for opened, closed in cells.held:
        inserts += [(text.at(opened), '<span class="kept-line">'), (text.at(closed), "</span>")]
    for point, mark in cells.gutters + cells.opened:
        inserts.append((after_name(point[0]) if point[1] is None else text.point(point), mark))
    # After the kept line's end at the same place, so the toggle stays the cell's own child.
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


# The shares of its region an identifier may take, besides the whole row's
# (`css/app/tables.css`, `--identifier-max`).
_ROOMS = frozenset({"", "shared", "snug"})


def _region(
    label: str,
    *,
    css: str = "",
    chart: bool = False,
    pane: bool = False,
    compare: bool = False,
    room: str = "",
    empty: str = "",
    remedy: Any = None,
    action_url: str = "",
    action_label: str = "",
    filtered: bool = False,
    clear: str = "",
) -> dict[str, Any]:
    """The region a table sits in: the box it fits, named by ``label``.

    An identifier in it is cut at a share of the region's width, and ``room``
    names a smaller one (docs/Design.md, A table is laid out before any script
    runs). A ``compare`` table sets two values side by side in each row, after
    the row's name, and they share the room evenly. Only a ``pane`` (a box of its own height, or a table sized to its
    content) scrolls in it. A ``chart``'s table fits its box as any table
    does, read whole with no row held back and in the chart's own order, so
    no heading sorts it. Every region is drawn as a stop for the keyboard;
    once script has run, only a pane, a chart's and one whose table scrolls
    stay one.

    Tables with no row are drawn as the one empty state instead
    (docs/Design.md, Nothing empty takes space): ``empty`` says what is
    missing, with what would fill it as ``remedy`` (a ``label`` and a
    ``url``) or ``action_url`` and ``action_label``. A list its own
    ``filtered`` search emptied says so instead, with the way back at
    ``clear``.
    """

    if room not in _ROOMS:
        raise ValueError(f"A region's room is one of {sorted(_ROOMS)}, not {room!r}.")
    classes = [
        *([css] if css else []),
        *(["is-pane"] if pane else []),
        *(["is-compare"] if compare else []),
        *([f"is-{room}"] if room else []),
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
    return {"label": label, "css": " ".join(classes), "chart": chart, "empty": nothing}


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
        # what it holds. Without script any region may scroll, so every one is
        # drawn as a stop, and tables.js takes it off a region whose table
        # fits, which holds nothing a reader needs to reach. It is a group and
        # no landmark: a landmark around it (its section, its card) already
        # has its place in the page's outline, often by the same name
        # (docs/Design.md, A table fits its box).
        # A table whose rows open an editor ({% row_editor %}) is a region of
        # the page's own (docs/Design.md, A read is a fragment), found by its
        # id in the page that answers it, so an editor opens, saves and closes
        # in place.
        edits = "data-row-edit" in content
        return format_html(
            '<div class="table-scroll{}"{}{}{}>{}</div>',
            f" {region['css']}" if region["css"] else "",
            format_html(' role="group" aria-label="{}" tabindex="0"', region["label"]),
            mark_safe(" data-chart") if region["chart"] else "",
            format_html(' id="table-{}" data-fragment=""', slugify(region["label"])) if edits else "",
            # The region's own content, rendered and escaped already; the mark
            # adds an attribute and nothing else.
            mark_safe(content),  # noqa: S308 - rendered and escaped already
        )


@register.tag("table_region")
@wraps(_region)
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


@register.inclusion_tag("partials/_table_heading.html", takes_context=True)
def table_heading(
    context: Any,
    label: str,
    kind: str = "",  # one of COLUMN_KINDS, or follows from css when left out
    *,
    css: str = "",
    drop: int = 0,
    sort: str = "",
    descending: str = "",
    local_sort: bool | None = None,  # left out, it follows from what the column holds
    hidden: bool = False,  # the label for assistive technology only
    title: str = "",
    gutter: bool = False,  # its column's cells stand in the gutter and hold each row's toggle
    select_all: bool = False,  # the tick-box that selects every row
) -> dict[str, Any]:
    """One column heading.

    ``drop`` is the order a table too wide for its region lets the column go
    in, 1 first, the rightmost of a rank first. ``sort`` names the field a
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
    classes = [css] if css else []
    classes += ["select-column"] if select_all else []
    classes += ["row-gutter"] if gutter else []
    return {
        "label": label,
        "kind": kind,
        "css": " ".join(classes),
        "drop": drop,
        "server_sort": _server_sort(context, sort, descending) if sort else None,
        "local_sort": local_sort,
        "hidden": hidden,
        "title": title,
        "select_all": select_all,
    }


@register.inclusion_tag("partials/_row_group.html")
def row_group(
    label: str, count: int | None = None, *, folds: str = "", href: str = "", attention: bool = False
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


@register.inclusion_tag("partials/_row_actions.html")
def row_actions(actions: Any = (), *, gap: Any = None, first: int = 0, put_away: Any = None) -> dict[str, Any]:
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
    of its buttons, drawn as they are, and never waiting in the open line."""

    actions = tuple(actions or ())
    shown, later = (actions[:first], actions[first:]) if first else (actions, ())
    return {"actions": shown, "later": later, "gap": gap, "put_away": put_away}


@register.inclusion_tag("partials/_row_toggle.html")
def row_toggle() -> dict[str, Any]:
    """The toggle that opens a row to all it holds, last in the cell that names
    the row, in a table with no headings; under a heading that keeps the gutter
    (``gutter=True``) the region draws it. The stylesheet shows it only where
    the row has something hidden."""

    return {}


@register.inclusion_tag("partials/_row_select.html")
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


# How many of a line list's parts size its column and wrap with it, as any
# value's words do; the rest show only as far as the line they end has room.
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
        # The rest is the box that clips; its run lays the parts out.
        if css:
            more = format_html('<li class="line-rest"><ul class="{} line-run">{}</ul></li>', css, tail) if rest else ""
            return format_html('<ul class="{} line-list">{}{}</ul>', css, parts, more)
        more = format_html('<span class="line-rest"><span class="line-run">{}</span></span>', tail) if rest else ""
        return format_html('<span class="line-list">{}{}</span>', parts, more)


@register.tag("line_list")
def line_list(parser: Any, token: Any) -> LineListNode:
    """``{% line_list row.tech_list as name %}{% status name chip=True %}{% endline_list %}``:
    a run of short parts in a cell, each drawn by the body, as many on its
    line as the line has room for; what does not fit waits for the row to
    open, and only then does the row offer to (tables.js measures the cut).
    ``list="dot-list"`` draws it as that list, the body drawing each ``li``.
    No count decides what shows: the layout does (docs/Design.md)."""

    bits = token.split_contents()
    if len(bits) not in (4, 5) or bits[2] != "as":
        raise template.TemplateSyntaxError('line_list takes values "as" name, then optionally list="<class>"')
    options = token_kwargs(bits[4:], parser)
    if len(options) != len(bits[4:]) or set(options) - {"list"}:
        raise template.TemplateSyntaxError('line_list takes one option, list="<class>"')
    nodelist = parser.parse(("endline_list",))
    parser.delete_first_token()
    return LineListNode(nodelist, parser.compile_filter(bits[1]), bits[3], options.get("list"))
