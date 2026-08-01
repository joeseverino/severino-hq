"""Canonical search use case shared by web, CLI, TUI, and MCP adapters."""

from __future__ import annotations

from django.db import connection
from django.db.models import Case, IntegerField, Q, QuerySet, When

from search_index.backends import search_backend
from search_index.registry import BY_SCOPE

MAX_SEARCH_RESULTS = 5000
# Precise relevance ordering only matters for results a human will actually
# scan. Ranking every match would compile one CASE branch per id — thousands
# of bound parameters per query for ordering nobody sees past the first pages.
RELEVANCE_WINDOW = 500


class UnknownSearchScope(ValueError):
    pass


_fts5_ready = False


def _fts5_available() -> bool:
    """Cache only a positive probe.

    A negative result must be re-checked: if the first call in a process ever
    lands before the FTS table exists (fresh database, mid-migrate), a cached
    False would silently pin that process to the icontains fallback until
    restart.
    """
    global _fts5_ready
    if not _fts5_ready:
        _fts5_ready = (
            connection.vendor == "sqlite"
            and "search_index_fts" in connection.introspection.table_names()
        )
    return _fts5_ready


def search_ids(scope: str, query: str, *, limit: int = 100) -> list[str]:
    """Return relevance-ordered stable identifiers from the configured backend."""
    if scope not in BY_SCOPE:
        raise UnknownSearchScope(f"Unknown search scope {scope!r}.")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    capped_limit = min(limit, MAX_SEARCH_RESULTS)
    if _fts5_available():
        return search_backend.search(scope=scope, query=query, limit=capped_limit)

    definition = BY_SCOPE[scope]
    terms = query.split()[:8]
    queryset = definition.model.objects.all()
    for term in terms:
        predicate = Q()
        for field in definition.fields:
            predicate |= Q(**{f"{field}__icontains": term})
        queryset = queryset.filter(predicate)
    return [
        str(identifier)
        for identifier in queryset.values_list(
            definition.identifier_field,
            flat=True,
        )[:capped_limit]
    ]


def apply_search(queryset: QuerySet, *, scope: str, query: str) -> QuerySet:
    """Filter a domain queryset while retaining relevance as its default order."""
    object_ids = search_ids(scope, query, limit=MAX_SEARCH_RESULTS)
    if not object_ids:
        return queryset.none()
    definition = BY_SCOPE[scope]
    identifier_field = definition.identifier_field
    # Rank the head of the result set exactly; everything past the window
    # shares one bucket and falls back to the pk tiebreak the table layer
    # appends, so pagination stays deterministic without a giant CASE.
    relevance = Case(
        *[
            When(**{identifier_field: object_id, "then": position})
            for position, object_id in enumerate(object_ids[:RELEVANCE_WINDOW])
        ],
        default=RELEVANCE_WINDOW,
        output_field=IntegerField(),
    )
    return queryset.filter(
        **{f"{identifier_field}__in": object_ids}
    ).annotate(_search_rank=relevance)


def search_records(scope: str, query: str, *, limit: int = 50) -> dict:
    """Adapter-neutral JSON result for CLI, TUI, and remote delivery surfaces."""
    definition = BY_SCOPE.get(scope)
    if definition is None:
        raise UnknownSearchScope(f"Unknown search scope {scope!r}.")
    object_ids = search_ids(scope, query, limit=limit)
    model_field = (
        definition.model._meta.pk
        if definition.identifier_field == "pk"
        else definition.model._meta.get_field(definition.identifier_field)
    )
    typed_ids = [model_field.to_python(object_id) for object_id in object_ids]
    records = (
        definition.model.objects.in_bulk(typed_ids)
        if definition.identifier_field == "pk"
        else definition.model.objects.in_bulk(
            typed_ids,
            field_name=definition.identifier_field,
        )
    )
    return {
        "scope": scope,
        "query": query,
        "items": [
            {"id": object_id, "label": str(records[typed_id])}
            for object_id, typed_id in zip(object_ids, typed_ids, strict=True)
            if typed_id in records
        ],
    }
