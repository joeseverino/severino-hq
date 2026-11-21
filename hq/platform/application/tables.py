"""A table's columns and cells: how each column sets, sorts and leaves a narrow box."""

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Model

from hq.platform.core.table_layout import SAYS_NOTHING, leaving_order
lazy from hq.platform.core.included_parts import READING_AGE
lazy from hq.platform.core.templatetags import status_tags, table_tags

from .display_words import display
lazy from .freshness import last_read

if TYPE_CHECKING:
    from hq.platform.core.template_parts import Part

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


def ranked(columns: tuple[TableColumn, ...]) -> tuple[TableColumn, ...]:
    """The columns, each with the order a narrow table lets it go in.

    Ranks a list declares stand as declared. Otherwise they follow one rule:
    ``optional-col`` columns first, then the rest, each from the far end; never
    the column that names the row, a key column, a column of the row's
    controls, which stand beneath its words in a narrow box instead, or the
    closing figure of a table that marks no key.
    """

    if columns and columns[0].drop:
        raise ValueError(f"Column {columns[0].label!r} names each row, so it has no drop rank.")
    if any(column.drop for column in columns):
        return columns
    order = leaving_order([(column.kind, column.css.split()) for column in columns])
    rank = {index: place for place, index in enumerate(order, 1)}
    return tuple(replace(column, drop=rank.get(index, 0)) for index, column in enumerate(columns))


# Where a value of a cell shows: on its row's line (""), only once the row
# opens ("more"), only in a narrow box ("narrow"), or on the line until a
# narrow box lets it go to the open line ("narrow-more").
type Shows = Literal["", "more", "narrow", "narrow-more"]
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
    # Words after it, inside its link and its bold: what follows a key that
    # names the row ("and 3 more").
    after: str = ""
    shows: Shows = ""
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

    def __post_init__(self) -> None:
        values = [bool(self.text or self.url or self.link), self.moment is not None, self.amount is not None]
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

    @property
    def says_nothing(self) -> bool:
        """Whether it shows nothing: no value, nothing after it, nothing beneath."""

        return (
            not (self.url or self.link or not SAYS_NOTHING.fullmatch(self.text))
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", column_kind(self.kind, self.css, otherwise=""))
        check_drop(self.drop, self.css, self.label)


# A ``data-`` attribute's name after ``data-``.
_DATA_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


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
    # reader's order, first of its controls (``{% row_actions %}``).
    mark: RowMark | None = None
    order: RowOrder | None = None
    # What stands for its actions where the connection cannot make them (a
    # ``WriteGap``), and the reader's own way to set it aside, last of them.
    gap: Any = None
    put_away: PageAction | None = None
    # It asks to be weighed first, washed in the attention tone: a change
    # that lets a record do more.
    attention: bool = False

    def __post_init__(self) -> None:
        if not all(_DATA_NAME.fullmatch(name) for name in self.data):
            raise ValueError(f"A row's data names are lowercase words joined by hyphens, never {sorted(self.data)!r}.")

    @property
    def controls(self) -> bool:
        """Whether it ends with a cell of controls."""

        return bool(self.actions or self.mark or self.order or self.gap or self.put_away)


@dataclass(frozen=True, slots=True)
class Group:
    """Rows under one heading (``{% row_group %}``): its name, how many, the
    id its rows fold under when it starts folded away, the page they are
    dealt with on together, and whether a reader should weigh it first."""

    label: str
    rows: tuple[Row, ...]
    count: int | None = None
    folds: str = ""
    href: str = ""
    attention: bool = False


@dataclass(frozen=True, slots=True)
class DataTable:
    """A table drawn whole by ``{% data_table %}``: its columns ranked to leave
    a narrow box as a list's do, then its rows, or its groups of rows. A list
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
    # The row of its totals, drawn last, in its ``tfoot``.
    footer: Row | None = None
    # What ``{% table_region %}`` takes beside its label: its box, and what
    # it says with no rows.
    region: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rows and self.groups:
            raise ValueError("A table holds rows or groups of them, not both.")
        object.__setattr__(self, "columns", ranked(self.columns))
        if {"declared", "compare"} & set(self.region):
            raise ValueError("A declared table's region is declared already, and a change's is the change table's.")
        bound = table_tags.REGION.bind(self.label, **self.region)
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

    @property
    def has_actions(self) -> bool:
        return any(row.controls for group in self.bodies for row in group.rows)
