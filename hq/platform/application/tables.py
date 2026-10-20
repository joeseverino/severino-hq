"""Declarative query engine for searchable, filterable list views."""

import shlex
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal

from django.db.models import Q, QuerySet, Sum

lazy from hq.platform.application.search import apply_search

from .page_reads import Remedy
from .projection import guarded
lazy from . import security


@dataclass(frozen=True, slots=True)
class TableFilter:
    name: str
    label: str
    lookup: str
    choices: Iterable[tuple[Any, str]]


@dataclass(frozen=True, slots=True)
class TableSort:
    value: str
    label: str
    ordering: str | tuple[str, ...]


# What a column holds, which says how its cells set: prose wraps at words, an
# identifier is capped and breaks only at its seams, a number and a status keep
# their line (a number right-aligned in tabular figures), and a control column
# holds a tick-box or a row's actions and is never sorted or dropped.
COLUMN_KINDS = ("prose", "identifier", "numeric", "status", "control")

# The kind a column's classes already say, so it is not declared twice.
_KIND_OF_CLASS = {"num-col": "numeric", "date-col": "status", "actions-col": "control", "row-actions": "control"}


def column_kind(kind: str, css: str) -> str:
    """The column's kind: the one declared, else the one its classes say, else prose."""

    if kind:
        if kind not in COLUMN_KINDS:
            raise ValueError(f"A column holds {', '.join(COLUMN_KINDS)}, never {kind!r}.")
        return kind
    return next((_KIND_OF_CLASS[name] for name in css.split() if name in _KIND_OF_CLASS), "prose")


def check_drop(drop: int, css: str, label: str) -> None:
    """A drop rank is an order of leaving; a key column never leaves."""

    if drop < 0:
        raise ValueError(f"Column {label!r}: a drop rank is 1 or more, or 0 to keep it.")
    if drop and "key-col" in css.split():
        raise ValueError(f"Column {label!r} is kept at every width, so it has no drop rank.")


def named_first(columns: tuple[TableColumn, ...]) -> tuple[TableColumn, ...]:
    """The columns, refused when the one that names each row could be dropped."""

    if columns and columns[0].drop:
        raise ValueError(f"Column {columns[0].label!r} names each row, so it has no drop rank.")
    return columns


def ranked(columns: tuple[TableColumn, ...]) -> tuple[TableColumn, ...]:
    """The columns, each with the order a narrow table lets it go in.

    Ranks a list declares stand as declared. Otherwise they follow one rule:
    ``optional-col`` columns first, then the rest, each from the far end; never
    the column that names the row, a key column, or the closing figure of a
    table that marks no key.
    """

    columns = named_first(columns)
    if any(column.drop for column in columns):
        return columns
    keyed = any("key-col" in column.css.split() for column in columns)
    last = len(columns) - 1

    def spare(index: int, column: TableColumn) -> bool:
        closing_figure = not keyed and index == last and column.kind == "numeric"
        return index > 0 and "key-col" not in column.css.split() and not closing_figure

    far_first = [index for index in reversed(range(len(columns))) if spare(index, columns[index])]
    order = sorted(far_first, key=lambda index: "optional-col" not in columns[index].css.split())
    rank = {index: place for place, index in enumerate(order, 1)}
    return tuple(replace(column, drop=rank.get(index, 0)) for index, column in enumerate(columns))


@dataclass(frozen=True, slots=True)
class TableColumn:
    """One column: its heading, and the field it sorts by if it sorts.

    Declared once on the view. The list page draws the header from these, and
    when a view declares no ``table_sorts`` the sort menu comes from them too,
    so a column cannot sort one way in its header and another in the menu.

    A column of words names its two orders itself ("Status A–Z", "Status
    Z–A"). A date or a number says its own: "Oldest purchase", "Highest cost".
    """

    label: str
    sort: str = ""
    ascending_label: str = ""
    descending_label: str = ""
    # Classes for the heading. ``key-col`` keeps the column on a phone, where a
    # table of four or more shows only the column that names each row and the
    # ones marked key; the rest are listed when a row is expanded.
    css: str = ""
    # One of ``COLUMN_KINDS``; left empty, it follows from ``css``.
    kind: str = ""
    # The order a narrow table lets the column go in, 1 first; 0 keeps it.
    # Never on the column that names the row or on a key column.
    drop: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", column_kind(self.kind, self.css))
        check_drop(self.drop, self.css, self.label)

    def sorts(self) -> tuple[TableSort, ...]:
        if not self.sort:
            return ()
        return (
            TableSort(self.sort, self.ascending_label or f"{self.label} A–Z", self.sort),
            TableSort(
                f"-{self.sort}",
                self.descending_label or f"{self.label} Z–A",
                f"-{self.sort}",
            ),
        )


@dataclass(frozen=True, slots=True)
class TableToggle:
    name: str
    label: str


@dataclass(frozen=True, slots=True)
class TableTotal:
    """A column to sum, and where the sum belongs.

    ``column`` is the field being totalled and doubles as the key a template
    looks the value up by, so a totals row lands under the column it totals
    rather than being positioned by hand.
    """

    column: str
    label: str = "Total"


class TableListMixin:
    """Apply one stable URL/query contract to a Django ``ListView``."""

    table_search_fields: tuple[str, ...] = ()
    table_search_scope = ""
    table_filters: tuple[TableFilter, ...] = ()
    table_columns: tuple[TableColumn, ...] = ()
    # How many columns, taken by rank, leave in a mid-width box and in a narrow
    # one: `{% table_region %}`'s ``mid`` and ``narrow``.
    table_drop_mid = 0
    table_drop_narrow = 0
    # How much of a row an identifier in it may take: `{% table_region %}`'s
    # ``room`` ("shared" beside a wide prose column, "snug" beside other
    # identifiers).
    table_room: Literal["", "shared", "snug"] = ""
    # A checkbox per row, for copying the selected rows' ids.
    table_selectable = False
    table_sorts: tuple[TableSort, ...] = ()
    table_toggles: tuple[TableToggle, ...] = ()
    table_totals: tuple[TableTotal, ...] = ()
    table_default_sort = ""
    table_search_placeholder = "Search…"
    # What an empty list means, in one line ("No assets yet."). What would
    # fill it is derived: the page's lead action, or the connection that reads
    # what it lists (``Page.remedy``).
    table_empty = "Nothing here yet."
    # A sentence that qualifies it, in the same line ("Add one in the vault.").
    table_empty_why = ""
    # False where the lead action is a view of this same list, not a way to fill it.
    table_empty_remedy = True

    def get_table_empty(self) -> str:
        return self.table_empty

    def get_table_empty_remedy(self) -> Remedy | None:
        """What fills this list when it is empty, where the page's derived remedy
        is not it. None takes the derived one."""

        return None

    def get_table_filters(self) -> tuple[TableFilter, ...]:
        return self.table_filters

    def resolved_table_filters(self) -> tuple[TableFilter, ...]:
        if not hasattr(self, "_resolved_table_filters"):
            self._resolved_table_filters = tuple(self.get_table_filters())
        return self._resolved_table_filters

    def get_table_sorts(self) -> tuple[TableSort, ...]:
        if self.table_sorts:
            return self.table_sorts
        return tuple(sort for column in self.table_columns for sort in column.sorts())

    def table_values(self, name: str) -> list[str]:
        return [value for value in self.request.GET.getlist(name) if value]

    def _apply_table_search(self, queryset: QuerySet, query: str) -> QuerySet:
        if query and self.table_search_scope:
            return apply_search(
                queryset,
                scope=self.table_search_scope,
                query=query,
                principal=security.web_principal(self.request.user),
            )
        if query and self.table_search_fields:
            try:
                terms = shlex.split(query)
            except ValueError:
                terms = query.split()
            for term in terms[:8]:
                predicate = Q()
                for field in self.table_search_fields:
                    predicate |= Q(**{f"{field}__icontains": term})
                queryset = queryset.filter(predicate)
        return queryset

    def _apply_table_filters(self, queryset: QuerySet) -> QuerySet:
        for spec in self.resolved_table_filters():
            values = self.table_values(spec.name)
            if values:
                queryset = queryset.filter(**{f"{spec.lookup}__in": values})
        return queryset

    def _apply_table_sort(self, queryset: QuerySet, query: str) -> QuerySet:
        requested_sort = self.request.GET.get("sort")
        selected_sort = requested_sort or self.table_default_sort
        if query and self.table_search_scope and requested_sort in (None, "_relevance"):
            return queryset.order_by("_search_rank", "pk")
        sort = next(
            (spec for spec in self.get_table_sorts() if spec.value == selected_sort),
            None,
        )
        if sort:
            ordering = sort.ordering if isinstance(sort.ordering, tuple) else (sort.ordering,)
            # Stable pagination: equal values must not drift between pages.
            if not any(field.lstrip("-") == "pk" for field in ordering):
                ordering = (*ordering, "pk")
            queryset = queryset.order_by(*ordering)
        return queryset

    def apply_table_query(self, queryset: QuerySet) -> QuerySet:
        """Apply the shared search, filter, and stable-sort URL contract."""

        query = self.request.GET.get("q", "").strip()
        queryset = self._apply_table_search(queryset, query)
        queryset = self._apply_table_filters(queryset)
        return guarded(self._apply_table_sort(queryset, query))

    def table_context(self) -> dict[str, Any]:
        filters = []
        active_count = 0
        for spec in self.resolved_table_filters():
            selected = self.table_values(spec.name)
            active_count += len(selected)
            filters.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "selected_count": len(selected),
                    "options": [
                        {
                            "value": str(value),
                            "label": label,
                            "selected": str(value) in selected,
                        }
                        for value, label in spec.choices
                    ],
                }
            )
        toggles = []
        for toggle in self.table_toggles:
            selected = bool(self.request.GET.get(toggle.name))
            active_count += int(selected)
            toggles.append({"name": toggle.name, "label": toggle.label, "selected": selected})
        query = self.request.GET.get("q", "")
        active_count += int(bool(query.strip()))
        query_params = self.request.GET.copy()
        query_params.pop("page", None)
        return {
            "query": query,
            "search_placeholder": self.table_search_placeholder,
            "filters": filters,
            "sorts": self.get_table_sorts(),
            "selected_sort": (
                "_relevance"
                if query and self.request.GET.get("sort") in (None, "_relevance")
                else self.request.GET.get("sort", self.table_default_sort)
            ),
            "toggles": toggles,
            # A rank says when a column leaves; with no count it never would.
            "columns": (
                ranked(self.table_columns)
                if self.table_drop_mid or self.table_drop_narrow
                else named_first(self.table_columns)
            ),
            "drop": {"mid": self.table_drop_mid, "narrow": self.table_drop_narrow},
            "room": self.table_room,
            "selectable": self.table_selectable,
            "active_count": active_count,
            "querystring": query_params.urlencode(),
            "empty": self.get_table_empty(),
            "empty_why": self.table_empty_why,
            "empty_remedy": self.table_empty_remedy,
            "empty_link": self.get_table_empty_remedy(),
            "clear": Remedy("Clear the search and filters", "?"),
        }

    def table_totals_for(self, queryset: QuerySet[Any]) -> dict[str, Any]:
        """Sum the declared columns over the whole filtered set.

        Over the queryset, deliberately, and never over the page. A total that
        silently describes the twenty rows currently visible is worse than no
        total: it is a number that changes with the page and looks like a
        fact about the filter.
        """
        if not self.table_totals or queryset is None:
            return {}
        sums = queryset.aggregate(
            **{f"total_{index}": Sum(spec.column) for index, spec in enumerate(self.table_totals)}
        )
        return {
            spec.column: {
                "label": spec.label,
                "value": sums.get(f"total_{index}"),
            }
            for index, spec in enumerate(self.table_totals)
        }

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["table"] = self.table_context()
        # `object_list` is the filtered, ordered queryset before the paginator
        # slices it, which is exactly the set a total should describe.
        context["table_totals"] = self.table_totals_for(getattr(self, "object_list", None))
        return context
