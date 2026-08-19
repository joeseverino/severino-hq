"""Canonical search use case shared by web, CLI, TUI, and MCP adapters."""

from __future__ import annotations

from django.db import connection
from django.db.models import Case, IntegerField, Q, QuerySet, When
from django.db.models.expressions import RawSQL

from search_index.backends import SnippetParts, search_backend
from application.search_contracts import SearchDefinition
from search_index.registry import BY_SCOPE

from .projection import page_size
from .security import AuthorizationError, Capability, Principal

MAX_SEARCH_RESULTS = 5000
# Precise relevance ordering only matters for results a human will actually
# scan. Ranking every match would compile one CASE branch per id — thousands
# of bound parameters per query for ordering nobody sees past the first pages.
RELEVANCE_WINDOW = 500

# Every scope is readable with the baseline READ capability except the audit
# trail: it is a security log, and free-text search over it is strictly more
# revealing than the bounded recent_activity projection MCP already gets.
SCOPE_CAPABILITIES = {
    scope: Capability.READ for scope in BY_SCOPE
} | {"audit": Capability.READ_AUDIT_LOG}


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


def _authorize(scope: str, principal: Principal) -> None:
    if scope not in BY_SCOPE:
        raise UnknownSearchScope(f"Unknown search scope {scope!r}.")
    principal.require(SCOPE_CAPABILITIES[scope])


def search_ids(
    scope: str, query: str, *, principal: Principal, limit: int = 100
) -> list[str]:
    """Return relevance-ordered stable identifiers from the configured backend."""
    _authorize(scope, principal)
    # Its own ceiling, the shared rule. Search caps lower than a listing does
    # because relevance past a hundred hits is noise, but "a page must be at
    # least one row" is not a different rule here than anywhere else.
    capped_limit = page_size(limit, maximum=MAX_SEARCH_RESULTS)
    if _fts5_available():
        return search_backend.search(scope=scope, query=query, limit=capped_limit)
    return _fallback_ids(BY_SCOPE[scope], query, capped_limit)


def apply_search(
    queryset: QuerySet, *, scope: str, query: str, principal: Principal
) -> QuerySet:
    """Filter a domain queryset while retaining relevance as its default order."""
    head_ids = search_ids(scope, query, principal=principal, limit=RELEVANCE_WINDOW)
    if not head_ids:
        return queryset.none()
    definition = BY_SCOPE[scope]
    identifier_field = definition.identifier_field
    # Rank the head of the result set exactly; everything past the window
    # shares one bucket and falls back to the pk tiebreak the table layer
    # appends, so pagination stays deterministic without a giant CASE.
    relevance = Case(
        *[
            When(**{identifier_field: object_id, "then": position})
            for position, object_id in enumerate(head_ids)
        ],
        default=RELEVANCE_WINDOW,
        output_field=IntegerField(),
    )
    if _fts5_available():
        # Membership via an FTS subquery: three bound parameters regardless of
        # match count, instead of one parameter per id in a giant IN list.
        sql, params = search_backend.search_sql(
            scope=scope, query=query, limit=MAX_SEARCH_RESULTS
        )
        membership = RawSQL(sql, params)
    else:
        membership = search_ids(
            scope, query, principal=principal, limit=MAX_SEARCH_RESULTS
        )
    return queryset.filter(
        **{f"{identifier_field}__in": membership}
    ).annotate(_search_rank=relevance)


def _fallback_snippet(definition: SearchDefinition, instance, query: str) -> SnippetParts:
    """Portable snippet: a window around the first matched term, term marked."""
    body = definition.body(instance).replace("\n", " ")
    lowered = body.lower()
    for term in query.split()[:8]:
        position = lowered.find(term.lower())
        if position < 0:
            continue
        start = max(0, position - 60)
        end = min(len(body), position + len(term) + 90)
        parts: SnippetParts = []
        if start > 0:
            parts.append(("… ", False))
        if position > start:
            parts.append((body[start:position], False))
        parts.append((body[position:position + len(term)], True))
        if end > position + len(term):
            parts.append((body[position + len(term):end], False))
        if end < len(body):
            parts.append((" …", False))
        return parts
    return [(body[:150], False)] if body else []


def _scope_hits(
    definition: SearchDefinition, query: str, limit: int
) -> list[tuple[str, SnippetParts | None]]:
    """Ranked (object_id, snippet) hits from whichever backend is active."""
    if _fts5_available():
        return search_backend.search_snippets(
            scope=definition.scope, query=query, limit=limit
        )
    return [(object_id, None) for object_id in _fallback_ids(definition, query, limit)]


def _fallback_ids(definition: SearchDefinition, query: str, limit: int) -> list[str]:
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
            definition.identifier_field, flat=True
        )[:limit]
    ]


def _fetch_records(definition: SearchDefinition, object_ids: list[str]) -> dict:
    """Map object_id -> instance, preserving typed-lookup semantics."""
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
            typed_ids, field_name=definition.identifier_field
        )
    )
    return {
        object_id: records[typed_id]
        for object_id, typed_id in zip(object_ids, typed_ids, strict=True)
        if typed_id in records
    }


def global_search(
    query: str, *, principal: Principal, limit_per_scope: int = 8
) -> dict:
    """Relevance-ranked, snippeted results across every scope the principal
    may search. Scopes the principal lacks (e.g. the audit log for a
    least-privilege adapter) are omitted entirely, not shown empty."""
    groups = []
    total = 0
    for scope, definition in BY_SCOPE.items():
        try:
            principal.require(SCOPE_CAPABILITIES[scope])
        except AuthorizationError:
            continue
        hits = _scope_hits(definition, query, limit_per_scope)
        records = _fetch_records(definition, [object_id for object_id, _ in hits])
        items = []
        for object_id, snippet in hits:
            record = records.get(object_id)
            if record is None:
                continue
            if snippet is None:
                snippet = _fallback_snippet(definition, record, query)
            items.append(
                {
                    "id": object_id,
                    "title": definition.title(record),
                    "badge": definition.badge(record),
                    "url": (
                        record.get_absolute_url()
                        if hasattr(record, "get_absolute_url")
                        else ""
                    ),
                    "timestamp": definition.timestamp(record),
                    "snippet": snippet,
                }
            )
        groups.append(
            {
                "scope": scope,
                "label": definition.label,
                "count": len(items),
                "items": items,
            }
        )
        total += len(items)
    return {"query": query, "total": total, "groups": groups}


def search_records(
    scope: str, query: str, *, principal: Principal, limit: int = 50
) -> dict:
    """Adapter-neutral JSON result for CLI, TUI, and remote delivery surfaces."""
    _authorize(scope, principal)
    definition = BY_SCOPE[scope]
    hits = _scope_hits(definition, query, limit)
    records = _fetch_records(definition, [object_id for object_id, _ in hits])
    items = []
    for object_id, snippet in hits:
        record = records.get(object_id)
        if record is None:
            continue
        if snippet is None:
            snippet = _fallback_snippet(definition, record, query)
        items.append(
            {
                "id": object_id,
                "label": str(record),
                "title": definition.title(record),
                "snippet": "".join(text for text, _ in snippet),
            }
        )
    return {"scope": scope, "query": query, "items": items}
