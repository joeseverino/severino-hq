"""The tags a data table is declared through.

``{% table_region %}`` wraps the table in its labelled scroll region,
``{% table_heading %}`` draws each column heading with its kind, the box width
it leaves at, and its sort control, and ``{% row_toggle %}`` draws the
disclosure of a row that holds more than it shows. All of it is in the HTML on
first paint, so nothing about a table's shape waits on a script: the
stylesheet drops a column by its heading's tier, and shows a row's toggle
where the row has something hidden.
"""

import inspect
import re
from functools import wraps
from html.parser import HTMLParser
from typing import Any

from django import template
from django.template.base import token_kwargs
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from hq.platform.application.tables import check_drop, column_kind

register = template.Library()

# Where a region's narrow and mid-width boxes end is the stylesheet's
# (`app.css`, the table region's container queries); a heading names only the
# widest box it leaves in.
TIERS = ("mid", "narrow")
_DROPS = "_table_drops"

# A cell that says nothing: dashes and space, and nothing drawn but the empty
# value's own mark.
_SAYS_NOTHING = re.compile(r"[\s\u2013\u2014-]*")


class _BlankCells(HTMLParser):
    """Where each body cell of a region's table starts, for every cell that says
    nothing. Only the region's own table: a table inside a cell is that cell's
    content, and says something."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables = 0
        self.body = False
        self.cell: list[Any] | None = None
        self.blank: list[tuple[int, int]] = []
        # Inside the empty value's mark: its tag and how deep.
        self.mark: list[Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cell is not None and self.tables == 1 and tag in ("td", "th"):
            self._close()
        if self.cell is not None:
            self._inside(tag, attrs)
            if tag == "table":
                self.tables += 1
            return
        if tag == "table":
            self.tables += 1
        elif self.tables == 1 and tag in ("tbody", "thead", "tfoot"):
            self.body = tag == "tbody"
        elif self.tables == 1 and self.body and tag in ("td", "th"):
            self.cell = [self.getpos(), True]

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cell is not None:
            self._inside(tag, attrs, void=True)

    def _inside(self, tag: str, attrs: list[tuple[str, str | None]], *, void: bool = False) -> None:
        if self.mark is not None:
            if tag == self.mark[0] and not void:
                self.mark[1] += 1
            return
        if "empty-value" in (dict(attrs).get("class") or "").split():
            if not void and tag not in _VOID:
                self.mark = [tag, 1]
            return
        if tag != "br":
            assert self.cell is not None
            self.cell[1] = False

    def handle_endtag(self, tag: str) -> None:
        if self.mark is not None and tag == self.mark[0]:
            self.mark[1] -= 1
            if not self.mark[1]:
                self.mark = None
            return
        if tag == "table":
            if self.cell is not None and self.tables == 1:
                self._close()
            self.tables -= 1
        elif self.tables == 1 and self.cell is not None and tag in ("td", "th", "tr", "tbody"):
            self._close()
        if self.tables == 1 and tag == "tbody":
            self.body = False

    def handle_data(self, data: str) -> None:
        if self.cell is not None and self.mark is None and not _SAYS_NOTHING.fullmatch(data):
            self.cell[1] = False

    def _close(self) -> None:
        assert self.cell is not None
        if self.cell[1]:
            self.blank.append(self.cell[0])
        self.cell = None
        self.mark = None


_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


def mark_blank_cells(markup: str) -> str:
    """The table with each body cell that says nothing marked ``data-blank``, so
    the stylesheet leaves out a column blank in every row and does not count a
    blank cell as something a row holds back."""

    cells = _BlankCells()
    cells.feed(markup)
    cells.close()
    if not cells.blank:
        return markup
    # The parser counts lines by newline alone.
    lines = [0] + [found.end() for found in re.finditer("\n", markup)]
    starts = sorted(lines[row - 1] + column for row, column in cells.blank)
    parts, last = [], 0
    for start in starts:
        # `<td` or `<th`: the mark goes straight after the name.
        parts += [markup[last : start + 3], " data-blank"]
        last = start + 3
    parts.append(markup[last:])
    return "".join(parts)


# The shares of its region an identifier may take, besides the whole row's
# (app.css, `--identifier-max`).
_ROOMS = frozenset({"", "shared", "snug"})


def _region(
    label: str,
    *,
    css: str = "",
    chart: bool = False,
    pane: bool = False,
    room: str = "",
    mid: int = 0,
    narrow: int = 0,
) -> dict[str, Any]:
    """The labelled region a table sits in: the container its tiers are
    measured against, and how many columns, by rank, leave in a mid-width box
    (``mid``) and in a narrow one (``narrow``). An identifier in it is cut at a
    share of the region's width; ``room`` names a smaller share for a row whose
    identifier sits beside wide columns (``shared``) or beside other
    identifiers (``snug``).

    A table fits its region, so the region is no scroll box and its heading
    pins under the page's chrome. Only a ``pane`` (a box of its own height, or
    a table sized to its content) and a ``chart``'s table scroll in it, and
    those are reachable from the keyboard."""

    # A count worked out from a table's width (`columns|length|add:-2`) is none
    # below nothing.
    counts = {"mid": max(0, int(mid or 0)), "narrow": max(0, int(narrow or 0))}
    if counts["narrow"] < counts["mid"]:
        raise ValueError("A narrow box drops at least the columns a mid-width one does.")
    if room not in _ROOMS:
        raise ValueError(f"A region's room is one of {sorted(_ROOMS)}, not {room!r}.")
    classes = [
        "table-scroll",
        *([css] if css else []),
        *(["is-pane"] if pane else []),
        *([f"is-{room}"] if room else []),
    ]
    return {"label": label, "css": " ".join(classes), "chart": chart, "scrolls": pane or chart, "counts": counts}


class RegionNode(template.Node):
    """``{% table_region %}``: what it encloses, in its region, with the region's
    counts in the context its headings are drawn in."""

    child_nodelists = ("nodelist",)

    def __init__(self, nodelist: Any, label: Any, options: dict[str, Any]) -> None:
        self.nodelist = nodelist
        self.label = label
        self.options = options

    def render(self, context: Any) -> str:
        region = _region(
            self.label.resolve(context), **{name: value.resolve(context) for name, value in self.options.items()}
        )
        with context.push({_DROPS: region["counts"]}):
            content = self.nodelist.render(context)
        return format_html(
            '<div class="{}" role="region" aria-label="{}"{}{}>{}</div>',
            region["css"],
            region["label"],
            mark_safe(' tabindex="0"') if region["scrolls"] else "",
            mark_safe(" data-chart") if region["chart"] else "",
            # The region's own content, rendered and escaped already; the mark
            # adds an attribute and nothing else.
            mark_safe(mark_blank_cells(content)),
        )


@register.tag("table_region")
@wraps(_region)
def table_region(parser: Any, token: Any) -> RegionNode:
    bits = token.split_contents()
    options = token_kwargs(bits[2:], parser)
    try:
        if len(bits) < 2 or len(options) != len(bits) - 2:
            raise TypeError("a label, then name=value")
        inspect.signature(_region).bind(bits[1], **options)
    except TypeError as wrong:
        raise template.TemplateSyntaxError(f"table_region takes {inspect.signature(_region)}: {wrong}") from wrong
    nodelist = parser.parse(("endtable_region",))
    parser.delete_first_token()
    return RegionNode(nodelist, parser.compile_filter(bits[1]), options)


def _server_sort(context: Any, ascending: str, descending: str) -> dict[str, str]:
    """A list's own sort for this column: where the link goes and which way it stands."""

    current = context["table"]["selected_sort"]
    descending = descending or f"-{ascending}"
    next_sort = descending if current == ascending else ascending
    params = context["request"].GET.copy()
    params["sort"] = next_sort
    params.pop("page", None)
    return {
        "url": f"?{params.urlencode()}",
        "direction": "ascending" if current == ascending else "descending" if current == descending else "none",
        "next_direction": "descending" if next_sort == descending else "ascending",
    }


def drop_tier(context: Any, rank: int) -> str:
    """The widest box a column of this rank leaves in, by its region's counts."""

    counts = context.get(_DROPS) or {}
    if rank and not any(counts.values()):
        raise ValueError(f"Drop rank {rank} is in a region that drops nothing: give the region its counts.")
    return next((tier for tier in TIERS if rank and rank <= counts.get(tier, 0)), "")


@register.inclusion_tag("partials/_table_heading.html", takes_context=True)
def table_heading(
    context: Any,
    label: str,
    kind: str = "",
    *,
    css: str = "",
    drop: int = 0,
    sort: str = "",
    descending: str = "",
    local_sort: bool = False,
    hidden: bool = False,
    title: str = "",
    gutter: bool = False,
    select_all: bool = False,
    note: str = "",
    note_css: str = "",
) -> dict[str, Any]:
    """One column heading.

    ``kind`` is one of ``COLUMN_KINDS``, or follows from ``css`` when left out.
    ``drop`` is the order a narrow table lets the column go in, 1 first; its
    region says how many go in each box. ``sort`` names the field a
    server-sorted list orders this column by (``descending`` when its reverse is
    not ``-field``); ``local_sort`` sorts the rows already on the page instead.
    ``hidden`` keeps the label for assistive technology only, ``gutter`` makes
    room for the row toggles beneath it, and ``select_all`` draws the tick-box
    that selects every row. ``note`` is a word beside the label that qualifies
    the whole column, set in ``note_css``.
    """

    if sort and local_sort:
        raise ValueError(f"Column {label!r} sorts on the server or on the page, not both.")
    kind = "control" if select_all else column_kind(kind, css)
    check_drop(drop, css, label)
    classes = [css] if css else []
    classes += ["select-column"] if select_all else []
    classes += ["row-gutter"] if gutter else []
    return {
        "label": label,
        "kind": kind,
        "css": " ".join(classes),
        "drop_tier": drop_tier(context, drop),
        "server_sort": _server_sort(context, sort, descending) if sort else None,
        "local_sort": local_sort,
        "hidden": hidden,
        "title": title,
        "select_all": select_all,
        "note": note,
        "note_css": note_css,
    }


@register.filter
def sortable(rows: Any) -> bool:
    """Whether the rows on a page are worth sorting there: two or more.
    ``local_sort=rows|sortable`` on each heading that sorts them, or the count
    of a table's rows where they come in groups (``local_sort=count|sortable``)."""

    return (rows if isinstance(rows, int) else len(rows)) > 1


@register.inclusion_tag("partials/_row_toggle.html")
def row_toggle() -> dict[str, Any]:
    """The toggle that opens a row to all it holds, last in the cell that names the
    row; that cell, in every row of the table, and its heading make the gutter.
    The stylesheet shows it only where the row has something hidden."""

    return {}
