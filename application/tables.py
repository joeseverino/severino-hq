"""Declarative query engine for searchable, filterable list views."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Any, Iterable

from django.db.models import Q, QuerySet


@dataclass(frozen=True)
class TableFilter:
    name: str
    label: str
    lookup: str
    choices: Iterable[tuple[Any, str]]


@dataclass(frozen=True)
class TableSort:
    value: str
    label: str
    ordering: str | tuple[str, ...]


@dataclass(frozen=True)
class TableToggle:
    name: str
    label: str


class TableListMixin:
    """Apply one stable URL/query contract to a Django ``ListView``."""

    table_search_fields: tuple[str, ...] = ()
    table_search_scope = ""
    table_filters: tuple[TableFilter, ...] = ()
    table_sorts: tuple[TableSort, ...] = ()
    table_toggles: tuple[TableToggle, ...] = ()
    table_default_sort = ""
    table_search_placeholder = "Search…"

    def get_table_filters(self) -> tuple[TableFilter, ...]:
        return self.table_filters

    def resolved_table_filters(self) -> tuple[TableFilter, ...]:
        if not hasattr(self, "_resolved_table_filters"):
            self._resolved_table_filters = tuple(self.get_table_filters())
        return self._resolved_table_filters

    def get_table_sorts(self) -> tuple[TableSort, ...]:
        return self.table_sorts

    def table_values(self, name: str) -> list[str]:
        return [value for value in self.request.GET.getlist(name) if value]

    def apply_table_query(self, queryset: QuerySet) -> QuerySet:
        query = self.request.GET.get("q", "").strip()
        if query and self.table_search_scope:
            from application.search import apply_search

            queryset = apply_search(
                queryset,
                scope=self.table_search_scope,
                query=query,
            )
        elif query and self.table_search_fields:
            try:
                terms = shlex.split(query)
            except ValueError:
                terms = query.split()
            for term in terms[:8]:
                predicate = Q()
                for field in self.table_search_fields:
                    predicate |= Q(**{f"{field}__icontains": term})
                queryset = queryset.filter(predicate)

        for spec in self.resolved_table_filters():
            values = self.table_values(spec.name)
            if values:
                queryset = queryset.filter(**{f"{spec.lookup}__in": values})

        requested_sort = self.request.GET.get("sort")
        selected_sort = requested_sort or self.table_default_sort
        if query and self.table_search_scope and requested_sort in (None, "_relevance"):
            queryset = queryset.order_by("_search_rank", "pk")
            return queryset
        sort = next(
            (spec for spec in self.get_table_sorts() if spec.value == selected_sort),
            None,
        )
        if sort:
            ordering = (
                sort.ordering if isinstance(sort.ordering, tuple) else (sort.ordering,)
            )
            # Stable pagination: equal values must not drift between pages.
            if not any(field.lstrip("-") == "pk" for field in ordering):
                ordering = (*ordering, "pk")
            queryset = queryset.order_by(*ordering)
        return queryset

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
            toggles.append(
                {"name": toggle.name, "label": toggle.label, "selected": selected}
            )
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
            "active_count": active_count,
            "querystring": query_params.urlencode(),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["table"] = self.table_context()
        return context
