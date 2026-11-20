"""A table page drawn from one page of its resource's list read."""

from math import ceil
from typing import Any

from django.core.exceptions import BadRequest, ImproperlyConfigured

from .integration_specs import SEARCH_FIELD, ListRead, ResourceSpec
from .page_reads import Remedy
from .pages import PageMixin
from .tables import TableColumn, ranked
from .ui import ended
lazy from .resources import InvalidResourceInput, list_resource, resource_registry
lazy from .security import web_principal

# What a page's address calls the search its list reads as ``query``.
_SEARCH = "q"


class ListPageMixin(PageMixin):
    """The table page of a resource kind: one page of the list its API and MCP
    read answer (``resources.list_resource``), searched, filtered, sorted and
    paged there, so a page and an agent's list agree row for row. The address
    is the list's query, its search as ``q``; its toggles are the query's
    flags, each labeled by its title, and its sorts the list's own. The view
    declares how a row is drawn, never which rows."""

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
        """The list's query as the address asks it: each field it names, a page's worth."""

        asked: dict[str, Any] = {"limit": self.page_size}
        for name in self.listing.query.model_fields:
            value = self.request.GET.get(_SEARCH if name == SEARCH_FIELD else name, "").strip()
            if value and name != "limit":
                asked[name] = value
        return asked

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            self.collection = list_resource(
                self.resource, self.list_query(), principal=web_principal(request.user), strict=False
            )
        except InvalidResourceInput as exc:
            raise BadRequest(exc.reason) from exc
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return {**context, "object_list": self.collection["items"], "table": self.table(), "pages": self.pages()}

    def table(self) -> dict[str, Any]:
        """What the toolbar, the headings and the empty state draw."""

        listing, asked = self.listing, self.list_query()
        names = {sort.name for sort in listing.sorts}
        for column in self.table_columns:
            if column.sort and not {column.sort, f"-{column.sort}"} <= names:
                raise ImproperlyConfigured(
                    f"Column {column.label!r} sorts by {column.sort!r}, which its list never does."
                )
        toggles = [
            {"name": name, "label": field.title, "selected": bool(asked.get(name))}
            for name, field in listing.query.model_fields.items()
            if field.annotation is bool
        ]
        query = self.request.GET.get(_SEARCH, "")
        relevance = [("", "Relevance")] if query.strip() else []
        default = listing.sorts[0].name if listing.sorts and not relevance else ""
        return {
            "query": query,
            "search_placeholder": self.table_search_placeholder,
            "filters": [],
            "toggles": toggles,
            "sorts": listing.sorts,
            "sort_choices": [*relevance, *((sort.name, sort.label) for sort in listing.sorts)],
            "selected_sort": asked.get("sort", default),
            "columns": ranked(self.table_columns),
            "selectable": self.table_selectable,
            "identifier": self.spec.identifier,
            "active_count": sum(toggle["selected"] for toggle in toggles) + bool(query.strip()),
            "empty": self.table_empty or ended(f"No {self.spec.label.lower()}"),
            "empty_why": self.table_empty_why,
            "empty_remedy": self.table_empty_remedy,
            "empty_link": None,
            "clear": Remedy("Clear filters", "?"),
        }

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
