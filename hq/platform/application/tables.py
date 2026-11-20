"""A table's columns and cells: how each column sets, sorts and leaves a narrow box."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Model

from hq.platform.core.table_cells import leaving_order

from .display_words import display

if TYPE_CHECKING:
    from .entity_links import EntityLink
    from .pages import PageAction
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


@display
class Cell:
    """One value in a table, and where it goes if anywhere."""

    text: str
    url: str = ""
    # Leaves HQ. Rendered so the operator knows before clicking, and so the
    # linked page cannot reach back through window.opener.
    external: bool = False
    muted: bool = False
    # An entity mention, rendered through the link builder's answer.
    link: EntityLink | None = None
    # A state: ``text`` is its word and this its tone, drawn as a status.
    tone: str = ""

    @classmethod
    def of(cls, link: EntityLink, *, muted: bool = False) -> Cell:
        """A cell naming one entity, from ``entity_link``."""

        return cls(link.label, link.url, external=link.external, muted=muted, link=link)


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", column_kind(self.kind, self.css, otherwise=""))
        check_drop(self.drop, self.css, self.label)


@dataclass(frozen=True, slots=True)
class DataTable:
    """A table drawn whole by ``{% data_table %}``, its columns ranked to
    leave a narrow box as a list's do, and each row its cells, what its open
    line shows beneath it and what may be done to its record."""

    label: str
    columns: tuple[TableColumn, ...]
    rows: tuple[tuple[tuple[Cell, ...], RunEvidence | None, tuple[PageAction, ...]], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", ranked(self.columns))

    @classmethod
    def of(cls, label: str, columns: tuple[TableColumn, ...], records: Iterable[Any]) -> DataTable:
        """The table of ``records``, each cell the one its column declares."""

        return cls(label, columns, tuple((tuple(c.cell(r) for c in columns if c.cell), None, ()) for r in records))

    @property
    def has_actions(self) -> bool:
        return any(actions for _cells, _below, actions in self.rows)
