"""A table's columns and cells: how each column sets, sorts and leaves a narrow box."""

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Literal

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Model

from hq.platform.core.table_layout import SAYS_NOTHING, TRACKS
lazy from hq.platform.core.included_parts import METER, READING_AGE
lazy from hq.platform.core.templatetags import status_tags

from .describe import Describe
from .display_words import QUOTED, display
lazy from .freshness import last_read

if TYPE_CHECKING:
    from hq.platform.core.template_parts import Part

    from .asks import Ask
    from .entity_links import EntityLink
    from .freshness import Freshness
    from .pages import PageAction, RowMark, RowOrder
    from .tone import Toned
    from .work_runs import RunEvidence


# What a column holds, which says how its cells set: prose wraps at words, an
# identifier is capped and breaks only at its seams, a number and a status keep
# their line (a number right-aligned in tabular figures), and a control column
# holds a tick-box or a row's actions and is never sorted or dropped.
COLUMN_KINDS = ("prose", "identifier", "numeric", "status", "control")

# The kind a column's classes already say, so it is not declared twice.
_KIND_OF_CLASS = {"date-col": "status", "actions-col": "control", "row-actions": "control"}


def column_kind(kind: str, css: str, otherwise: str = "prose") -> str:
    """The column's kind: the one declared, else the one its classes say, else ``otherwise``."""

    if kind:
        if kind not in COLUMN_KINDS:
            raise ValueError(f"A column holds {', '.join(COLUMN_KINDS)}, never {kind!r}.")
        return kind
    return next((_KIND_OF_CLASS[name] for name in css.split() if name in _KIND_OF_CLASS), otherwise)


_STATUS_FIELDS = (models.BooleanField, models.DateField, models.TimeField, models.DurationField)
_NUMERIC_FIELDS = (models.IntegerField, models.FloatField, models.DecimalField)


def field_kind(model: type[Model] | None, path: str) -> str:
    """The kind of what a model field holds, followed along ``a__b``: a choice,
    a flag or a time is a status, a number is numeric, anything else (or a
    name no field answers to, such as an annotation) is prose."""

    field = None
    for name in path.split("__") if model and path else ():
        try:
            field = model._meta.get_field(name)
        except FieldDoesNotExist:
            return "prose"
        model = field.related_model
    if field is None or field.is_relation:
        return "prose"
    if field.choices or isinstance(field, _STATUS_FIELDS):
        return "status"
    return "numeric" if isinstance(field, _NUMERIC_FIELDS) else "prose"


def typed(columns: tuple[TableColumn, ...], model: type[Model] | None) -> tuple[TableColumn, ...]:
    """The columns, each that declares no kind given the kind of the field it sorts by."""

    return tuple(column if column.kind else replace(column, kind=field_kind(model, column.sort)) for column in columns)


def check_drop(drop: int, css: str, label: str) -> None:
    """A drop rank is an order of leaving; a key column never leaves."""

    if drop < 0:
        raise ValueError(f"Column {label!r}: a drop rank is 1 or more, or 0 to keep it.")
    if drop and "key-col" in css.split():
        raise ValueError(f"Column {label!r} is kept at every width, so it has no drop rank.")


# Where a value of a cell shows: on its row's line (""), only once the row
# opens ("more"), only in a narrow box ("narrow"), on the line until a
# narrow box lets it go to the open line ("narrow-more"), or as what the row
# means, quiet and cut at the line's end until the row opens ("gist").
type Shows = Literal["", "more", "narrow", "narrow-more", "gist"]
# What a value is beyond its column's kind: free text, which breaks wherever
# it must ("data"), or what the row means told in words ("prose").
type Role = Literal["", "data", "prose"]


@dataclass(frozen=True, slots=True, init=False)
class StatusMark:
    """A state drawn as ``status_pill`` draws it, given its own arguments,
    which are bound to its signature when the mark is made."""

    parts: tuple[Any, ...]
    options: Mapping[str, Any]

    def __init__(self, *parts: Any, **options: Any) -> None:
        bound = inspect.signature(status_tags.status_pill).bind(*parts, **options)
        object.__setattr__(self, "parts", tuple(bound.arguments.get("parts", ())))
        given = {name: value for name, value in bound.arguments.items() if name not in ("parts", "data")}
        object.__setattr__(self, "options", {**given, **bound.arguments.get("data", {})})

    @property
    def words(self) -> str:
        """What it says on its line: its words, or nothing for a mark alone."""

        if self.options.get("shown", True) is False:
            return ""
        return " ".join(str(getattr(part, "label", part)) for part in self.parts if part not in (None, ""))

    @property
    def pill(self) -> bool:
        """Whether it is drawn as a state's pill, which a column's drop order
        keeps as a state: never words in a tone, a dot's or a chip's name."""

        return bool(self.options.get("pill", True)) and not self.options.get("chip") and not self.options.get("dot")

    def __html__(self) -> str:
        return status_tags.status_pill(*self.parts, **self.options)


@dataclass(frozen=True, slots=True)
class Below:
    """A block its row shows only on its open line: under its label, or,
    with none, whole before the labeled ones. Each line one value."""

    label: str
    lines: tuple[Cell, ...]


@dataclass(frozen=True, slots=True, eq=False)
class Piece:
    """A declared part's partial drawn as a cell's value, given its variables.
    ``words`` are what it says on its line, which the layout reads for its
    length and emptiness, never its drawing."""

    part: Part
    context: Mapping[str, Any]
    words: str
    # It draws a field a reader fills in (a select), which fills its column.
    field: bool = False

    def __post_init__(self) -> None:
        if not self.part.partial:
            raise ValueError("A piece is a declared part's partial.")


@display
class Cell:
    """One cell of a table, said once: its value, drawn by what it is (words,
    a link, an entity, a state, a moment, an amount), then what follows it
    on its line and the lines beneath it. Everything the stylesheet reads off
    a cell (its emptiness, its length, whether it names the row or holds a
    control) is known from this before it is drawn."""

    text: str = ""
    url: str = ""
    # Leaves HQ. Rendered so the operator knows before clicking, and so the
    # linked page cannot reach back through window.opener.
    external: bool = False
    muted: bool = False
    # An entity mention, rendered through the link builder's answer.
    link: EntityLink | None = None
    # A state: ``text`` is its word and this its tone, drawn as a status.
    tone: str = ""
    # In bold, the name its row is read by.
    name: bool = False
    # It shows nothing at all, not even the empty value's mark: the side of a
    # change its row's record already says.
    blank: bool = False
    # It heads its row (``th scope="row"``), its label in the heading's voice.
    header: bool = False
    role: Role = ""
    # Its words as a readout draws them, an identifier in code; or as a line
    # its box cuts, read whole on a press (``{% cell_line %}``).
    readout: bool = False
    cut: bool = False
    # A record's own words in place of ``text``, shown as written, each
    # backticked span code (``code_spans``): a server's own description.
    quoted: str = field(default="", metadata=QUOTED)
    # Words after it, inside its link and its bold: what follows a key that
    # names the row ("and 3 more").
    after: str = ""
    shows: Shows = ""
    # Its line is its row's title, bold, each mark after it a box of its own
    # (`row-line`), as a line holding a gist always is.
    lead: bool = False
    # A moment, drawn as ``when`` draws it in ``form``, or an amount of money.
    moment: datetime | date | None = None
    form: str = ""
    amount: Decimal | None = None
    # A dot before it, joining it to what stands before it on its line.
    dot: bool = False
    # How many it stands for, after it: a group's rows, an event's changes.
    count: int | None = None
    # It leads with the toggle that opens the rows folded under its row.
    fold: bool = False
    # What follows it on its line, then its lines beneath, each one value.
    parts: tuple[Cell, ...] = ()
    # Its parts run as a line list draws them (``{% line_list %}``, dots):
    # the first few size the column, the rest show as far as the line holds.
    fit: bool = False
    lines: tuple[Cell, ...] = ()
    # A field of the page's form the reader fills in, standing in for a value.
    field: Any = None
    # The row's editor: the way in on the row's line, its form on its open line.
    editor: RowEditor | None = None
    # The value it sorts by on the page, where its words are not it.
    sort: str = ""
    # A state drawn as ``status_pill`` draws it, standing in for its words.
    status: StatusMark | None = None
    # What a press or a hover shows of it, and its ``title``.
    tip: str = ""
    title: str = ""
    # What a screen reader hears in place of its words ("ending in 1234").
    aloud: str = ""
    # The blocks its row shows only on its open line.
    below: tuple[Below, ...] = ()
    # A declared part drawn as its value.
    piece: Piece | None = None
    # Each ``data-`` attribute a script or a test finds its value by, by its
    # name after ``data-``: on its line's item, else around the value alone.
    # Read-only, since ``field`` here names its field of the page's form.
    data: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        held_data(self.data, "A cell")
        values = [
            bool(self.text or self.quoted or self.url or self.link),
            self.moment is not None,
            self.amount is not None,
        ]
        values += [self.status is not None, self.piece is not None]
        if sum(values) + (self.field is not None) + (self.editor is not None) > 1:
            raise ValueError(
                "A cell's value is one of words, a moment, an amount, a state, a piece, a field or an editor."
            )
        within = (*self.parts, *self.lines, *(line for block in self.below for line in block.lines))
        nested = [part.parts for part in self.parts] + [part.lines or part.below for part in within]
        held = [part.fold or part.field is not None or part.editor is not None for part in within]
        if any(nested) or any(held):
            raise ValueError("A cell's parts are each one value, and each of its lines one value and its parts.")
        beneath = within[len(self.parts) :]
        apart = (*(self.parts if self.fit else ()), *beneath, *(part for line in beneath for part in line.parts))
        if any(part.shows == "gist" for part in apart):
            raise ValueError("A gist follows the words of its cell's own line, never in a run or a line beneath.")

    @classmethod
    def of(cls, link: EntityLink, *, muted: bool = False, name: bool = False) -> Cell:
        """A cell naming one entity, from ``entity_link``."""

        return cls(link.label, link.url, external=link.external, muted=muted, link=link, name=name)

    @classmethod
    def state(cls, state: Toned) -> Cell:
        """A cell saying a subject's state in its own words and tone."""

        return cls(state.label, tone=str(state.tone))

    @classmethod
    def reading(
        cls, *, reading: Freshness | None = None, at: datetime | None = None, unread: str = "", muted: bool = False
    ) -> Cell:
        """A reading's age as every page words it (``partials/_reading_age.html``):
        of its ``Freshness``, of the moment ``at`` it was taken, else why there is none."""

        taken = reading.observed_at if reading is not None else at
        words = last_read(taken) if taken else (reading.label if reading is not None else unread or "No reading yet")
        context = {"reading": reading, "at": at, "unread": unread}
        return cls(piece=Piece(READING_AGE, context, words=words), muted=muted)

    @classmethod
    def meter(cls, marks: Iterable[Any], label: str) -> Cell:
        """A segment for each of ``marks`` in its tone, named and tipped by
        ``label``, as every meter draws them (``partials/_meter.html``)."""

        return cls(piece=Piece(METER, {"marks": tuple(marks), "label": label}, words=""))

    @property
    def says_nothing(self) -> bool:
        """Whether it shows nothing: no value, nothing after it, nothing beneath."""

        return (
            not (self.url or self.link or self.quoted or not SAYS_NOTHING.fullmatch(self.text))
            and self.moment is None
            and self.amount is None
            and self.count is None
            and not (self.status and self.status.parts)
            and not (self.piece and not SAYS_NOTHING.fullmatch(self.piece.words))
            and not (self.fold or self.field is not None or self.editor is not None)
            and all(part.says_nothing for part in (*self.parts, *self.lines))
        )


@display
class RowEditor:
    """A row's editor (``{% row_editor %}``): the form the view declares for the
    table's editors, the row's key, the words of the way in, where it posts,
    its button's words, and the row's values by field name."""

    form: Any
    key: Any
    label: str
    action: str
    verb: str
    initial: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TableColumn:
    """One column: its heading, and the sort of its list it is ordered by if
    it sorts, which its list read declares both ways (``ListSort.both``)."""

    # Left blank for a column of a row's controls, which shows no heading and
    # is named "Actions" to a screen reader.
    label: str
    sort: str = ""
    # Classes for the heading. ``key-col`` keeps the column at every width;
    # what a narrow box lets go is listed when a row is expanded.
    css: str = ""
    # One of ``COLUMN_KINDS``; left empty, it follows from ``css``, else from
    # the model field the column sorts by (``typed``), so a list declares it
    # only where neither says.
    kind: str = ""
    # The order a narrow table lets the column go in, 1 first; 0 keeps it.
    # Never on the column that names the row or on a key column.
    drop: int = 0
    # Its one plain value in a row, for a table drawn whole (``DataTable.of``).
    cell: Callable[[Any], Cell] | None = None
    # Its heading unseen, its words still said to a screen reader.
    hidden: bool = False
    # False for a column whose values have no order, such as prose, in a
    # table whose other columns sort on the page.
    sorts: bool = True
    # What a hover over its heading says of it (an agent's client).
    title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", column_kind(self.kind, self.css, otherwise=""))
        check_drop(self.drop, self.css, self.label)


# A ``data-`` attribute's name after ``data-``.
_DATA_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def held_data(data: Mapping[str, str], whose: str) -> None:
    """Refuse a ``data-`` attribute whose name an attribute cannot hold, so
    every declaration's names are written as they are."""

    if not all(_DATA_NAME.fullmatch(name) for name in data):
        raise ValueError(f"{whose}'s data names are lowercase words joined by hyphens, never {sorted(data)!r}.")


@dataclass(frozen=True, slots=True)
class Row:
    """One row: its cells, what may be done to its record, the run evidence its
    open line shows, its tick-box's key in a table whose rows are selected
    together, and how it folds: the id the rows folded under it name
    (``folds``), or the id of the row or group it is folded under."""

    cells: tuple[Cell, ...]
    actions: tuple[PageAction, ...] = ()
    evidence: RunEvidence | None = None
    key: Any = None
    folds: str = ""
    folded: str = ""
    # A row that asks to be read first, a message not yet read.
    emphasized: bool = False
    # Its one cell spans every column: the count of a fold's rest.
    across: bool = False
    # Its id, which a link to the row names, and each ``data-`` attribute a
    # script finds it by, by its name after ``data-``.
    id: str = ""
    data: Mapping[str, str] = field(default_factory=dict)
    # The reader's own mark on its record and the pair that moves it in the
    # reader's order, first of its controls (``partials/_row_actions.html``).
    mark: RowMark | None = None
    order: RowOrder | None = None
    # What stands for its actions where the connection cannot make them (a
    # ``WriteGap``), and the reader's own way to set it aside, last of them.
    gap: Any = None
    put_away: PageAction | None = None
    # Work a press asks for now, each after its actions (``partials/_ask.html``).
    asks: tuple[Ask, ...] = ()
    # It asks to be weighed first, washed in the attention tone: a change
    # that lets a record do more.
    attention: bool = False

    def __post_init__(self) -> None:
        held_data(self.data, "A row")

    @property
    def controls(self) -> bool:
        """Whether it ends with a cell of controls."""

        return bool(self.actions or self.mark or self.order or self.gap or self.put_away or self.asks)


@dataclass(frozen=True, slots=True)
class Group:
    """Rows under one heading (``partials/_row_group.html``): its name, how many, the
    id its rows fold under when it starts folded away, the page they are
    dealt with on together, and whether a reader should weigh it first."""

    label: str
    rows: tuple[Row, ...]
    count: int | None = None
    folds: str = ""
    href: str = ""
    attention: bool = False
    # Its body's id and each ``data-`` attribute a script finds it by, as a row's are.
    id: str = ""
    data: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        held_data(self.data, "A group")


def box(
    label: Annotated[str, Describe("What the table holds, the name its box is a stop by.")],
    *,
    css: Annotated[str, Describe("The classes of where it stands.")] = "",
    chart: Annotated[bool, Describe("A chart's table, read whole in the chart's order and scrolled.")] = False,
    pane: Annotated[bool, Describe("A box of its own height, which scrolls.")] = False,
    empty: Annotated[str, Describe("With no rows, what is missing.")] = "",
    remedy: Annotated[Any, Describe("With no rows, what would fill it: a `label` and a `url`.")] = None,
    action_url: Annotated[str, Describe("With no rows, where the link that fills it leads.")] = "",
    action_label: Annotated[str, Describe("That link's label.")] = "",
    filtered: Annotated[bool, Describe("Its own search narrows it, so no rows says nothing matches.")] = False,
    clear: Annotated[str, Describe("The way back from that search.")] = "",
) -> None:
    """The box a table stands in, named by ``label`` (``data_table``'s rules):
    the options ``DataTable.region`` binds."""


# The options a table's box takes, to which ``DataTable.region`` is bound.
REGION = inspect.signature(box)


@dataclass(frozen=True, slots=True)
class DataTable:
    """A table drawn whole by ``{% data_table %}``: its columns, each of which
    the region lets leave a narrow box by its rank or its own rule
    (``table_layout``), then its rows, or its groups of rows. A list
    its server sorts names the sort it stands in (``sorted_by``), and its
    headings sort there; any other table sorts on the page."""

    label: str
    columns: tuple[TableColumn, ...]
    rows: tuple[Row, ...] = ()
    groups: tuple[Group, ...] = ()
    selectable: bool = False
    sorted_by: str | None = None
    # Its rows stand in an order of their own, which no heading changes.
    keeps_order: bool = False
    # False for a table whose rows are the rest of what a part says (the
    # queue): no heading names its columns, which its rows declare.
    headed: bool = True
    # The row of its totals, drawn last, in its ``tfoot``.
    footer: Row | None = None
    # What its box takes beside its label (``box``): where it stands, and
    # what it says with no rows.
    region: Mapping[str, Any] = field(default_factory=dict)
    # Each ``data-`` attribute a script or a test finds the table by, by its
    # name after ``data-``, as a row's are.
    data: Mapping[str, str] = field(default_factory=dict)
    # In a table of one column with no headings (the queue), each row's
    # controls take their own width at its end rather than one shared down
    # the table, so a fold that opens moves no other row.
    controls_per_row: bool = False

    def __post_init__(self) -> None:
        held_data(self.data, "A table")
        if self.controls_per_row and (self.headed or len(self.columns) != 1):
            raise ValueError("Only a table of one column with no headings sizes each row's controls apart.")
        if self.rows and self.groups:
            raise ValueError("A table holds rows or groups of them, not both.")
        # A column past its grid's last track would be drawn over that one.
        if (drawn := len(self.columns) + self.has_actions) > TRACKS:
            raise ValueError(f"{self.label!r} draws {drawn} columns; a table holds {TRACKS}, its controls among them.")
        object.__setattr__(self, "columns", self._dated())
        bound = REGION.bind(self.label, **self.region)
        bound.apply_defaults()
        object.__setattr__(self, "region", {name: value for name, value in bound.arguments.items() if name != "label"})

    @classmethod
    def of(cls, label: str, columns: tuple[TableColumn, ...], records: Iterable[Any], **options: Any) -> DataTable:
        """The table of ``records``, each cell the one its column declares."""

        return cls(label, columns, tuple(Row(tuple(c.cell(r) for c in columns if c.cell)) for r in records), **options)

    @property
    def bodies(self) -> tuple[Group, ...]:
        """Its groups, or its rows as one group with no heading."""

        return self.groups or (Group("", self.rows),)

    def _dated(self) -> tuple[TableColumn, ...]:
        """Its columns, each whose rows each say a moment or nothing marked a
        date (``date-col``), which a row keeps as it keeps its state."""

        rows = [row for group in self.bodies for row in group.rows if not row.across]

        def dated(at: int) -> bool:
            cells = [row.cells[at] for row in rows if at < len(row.cells)]
            return any(cell.moment for cell in cells) and all(cell.moment or cell.says_nothing for cell in cells)

        return tuple(
            column
            if "date-col" in column.css.split() or not dated(at)
            else replace(column, css=f"{column.css} date-col".strip())
            for at, column in enumerate(self.columns)
        )

    @property
    def has_actions(self) -> bool:
        return any(row.controls for group in self.bodies for row in group.rows)
