"""A table page drawn from one page of its resource's list read."""

from math import ceil
from typing import Any

from django.core.exceptions import BadRequest, ImproperlyConfigured
from django.db.models import QuerySet

from .integration_specs import SEARCH_FIELD, ListRead, ResourceSpec
from .page_reads import Remedy
from .pages import PageMixin
from .tables import DataTable, Row, TableColumn, ranked, typed
from .ui import ended
lazy from .resources import InvalidResourceInput, list_resource, resource_registry
lazy from .security import web_principal

# What a page's address calls the search its list reads as ``query``.
_SEARCH = "q"


class ListPageMixin(PageMixin):
    """The table page of a resource kind: one page of the list its API and MCP
    read answer (``resources.list_resource``), searched, filtered, sorted and
    paged there, so a page and an agent's list agree row for row. The address
    is the list's query, its search as ``q``; its filters, toggles and sorts
    are the list's own. The view declares how a row is drawn, never which rows."""

    # The ``ResourceSpec`` whose list read draws the page, by its name.
    resource = ""
    # The headings, each that sorts naming one of the list's sorts.
    table_columns: tuple[TableColumn, ...] = ()
    page_size = 25
    # A checkbox per row, for copying the selected rows' identifiers.
    table_selectable = False
    table_search_placeholder = "Search…"
    # What an empty list means ("No receipts yet."), a sentence beside it, and
    # False where the page's lead action is not a way to fill it.
    table_empty = ""
    table_empty_why = ""
    table_empty_remedy = True
    collection: dict[str, Any]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.resource:
            raise ImproperlyConfigured(f"{cls.__name__} names the resource whose list it draws.")

    @property
    def spec(self) -> ResourceSpec:
        return resource_registry()[self.resource]

    @property
    def listing(self) -> ListRead:
        if (listing := self.spec.listing) is None:
            raise ImproperlyConfigured(f"{self.resource!r} has no list read for {type(self).__name__} to draw.")
        return listing

    def list_query(self) -> dict[str, Any]:
        """The list's query as the address asks it: each field it names, a
        filter's several values one list, a page's worth."""

        asked: dict[str, Any] = {"limit": self.page_size}
        for name in self.listing.query.model_fields:
            values = [value.strip() for value in self.request.GET.getlist(_SEARCH if name == SEARCH_FIELD else name)]
            if (value := ",".join(value for value in values if value)) and name != "limit":
                asked[name] = value
        for name, allowed in self.scope().items():
            if chosen := [value for value in asked.pop(name, "").split(",") if value in allowed]:
                asked[name] = ",".join(chosen)
        return asked

    def scope(self) -> dict[str, tuple[str, ...]]:
        """The values of a filter this page always reads within, each filter
        by its name: the address chooses among them, the list is read within
        all of them when it chooses none, and the toolbar offers only them."""

        return {}

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        scoped = {name: ",".join(allowed) for name, allowed in self.scope().items()}
        try:
            self.collection = list_resource(
                self.resource, scoped | self.list_query(), principal=web_principal(request.user), strict=False
            )
        except InvalidResourceInput as exc:
            raise BadRequest(exc.reason) from exc
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        rows = [self.row(item) for item in self.collection["items"]]
        table = self.table()
        if self.table_columns and all(column.cell for column in self.table_columns):
            table["drawn"] = self.drawn(context["page"].title, table, rows)
        return {**context, "object_list": rows, "table": table, "pages": self.pages()}

    def drawn(self, label: str, table: dict[str, Any], rows: list[Any]) -> DataTable:
        """The page's rows as its columns declare each cell, one page of the
        list's own order, each keyed for its tick-box where rows are selected."""

        key = self.spec.identifier
        return DataTable(
            label,
            table["columns"],
            tuple(
                Row(
                    tuple(column.cell(row) for column in table["columns"] if column.cell),
                    key=(row[key] if key else row.pk) if table["selectable"] else None,
                )
                for row in rows
            ),
            selectable=table["selectable"],
            sorted_by=table["selected_sort"],
        )

    def row(self, item: dict[str, Any]) -> Any:
        """One item as its row draws it: the item itself, or with what only
        the page says of it beside (a state as its pill takes it)."""

        return item

    def records(self, rows: QuerySet) -> list[Any]:
        """The page's records out of ``rows``, in the list's order, read in one
        query by the list's identifier: for a page that draws each row from its
        record (a form, a field the item leaves out) rather than from its item."""

        if not (key := self.spec.identifier):
            raise ImproperlyConfigured(f"{self.resource!r} names no identifier to read its records by.")
        ids = [str(item[key]) for item in self.collection["items"]]
        found = {str(pk): record for pk, record in rows.in_bulk(ids, field_name=key).items()}
        return [found[pk] for pk in ids]

    def table(self) -> dict[str, Any]:
        """What the toolbar, the headings and the empty state draw."""

        listing, asked = self.listing, self.list_query()
        names = {sort.name for sort in listing.sorts}
        for column in self.table_columns:
            if column.sort and not {column.sort, f"-{column.sort}"} <= names:
                raise ImproperlyConfigured(
                    f"Column {column.label!r} sorts by {column.sort!r}, which its list never does."
                )
        filters, scope = [], self.scope()
        for each in listing.filters:
            chosen = set(asked.get(each.name, "").split(",")) - {""}
            options = [
                {"value": value, "label": label, "selected": value in chosen}
                for value, label in each.options()
                if each.name not in scope or value in scope[each.name]
            ]
            # A filter with nothing to choose (a year before any record has
            # one, a page scoped to one value) is left out.
            if len(options) > (each.name in scope):
                filters.append(
                    {"name": each.name, "label": each.label, "selected_count": len(chosen), "options": options}
                )
        toggles = [
            {"name": toggle.name, "label": toggle.label, "selected": bool(asked.get(toggle.name))}
            for toggle in listing.toggles
        ]
        query = self.request.GET.get(_SEARCH, "")
        # A list that searches its own way keeps its order when searched.
        relevance = [("", "Relevance")] if query.strip() and self.spec.search else []
        default = listing.sorts[0].name if listing.sorts and not relevance else ""
        model = self.spec.search.model if self.spec.search else None
        return {
            "query": query,
            "search_placeholder": self.table_search_placeholder,
            "filters": filters,
            "toggles": toggles,
            "sorts": listing.sorts,
            "sort_choices": [*relevance, *((sort.name, sort.label) for sort in listing.sorts)],
            "selected_sort": asked.get("sort", default),
            "columns": ranked(typed(self.table_columns, model)),
            "selectable": self.table_selectable,
            "identifier": self.spec.identifier,
            "active_count": sum(item["selected_count"] for item in filters)
            + sum(toggle["selected"] for toggle in toggles)
            + bool(query.strip()),
            "empty": self.table_empty or ended(f"No {self.spec.label.lower()}"),
            "empty_why": self.table_empty_why,
            "empty_remedy": self.table_empty_remedy,
            "empty_link": self.get_table_empty_remedy(),
            "clear": Remedy("Clear filters", "?"),
        }

    def get_table_empty_remedy(self) -> Remedy | None:
        """What fills this list when it is empty, where the page's derived
        remedy is not it; None takes the derived one."""

        return None

    def pages(self) -> dict[str, Any] | None:
        """The turn to the pages beside this one and where it stands among
        them; none for a list one page holds."""

        read = self.collection
        if read["next"] is None and read["previous"] is None:
            return None
        return {
            "previous": read["previous"],
            "next": read["next"],
            "number": (read["previous"] or 0) + 1,
            "of": ceil(read["total"] / self.page_size) if read["total"] is not None else None,
        }
