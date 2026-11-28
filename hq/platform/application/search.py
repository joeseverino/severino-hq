"""Canonical search use case shared by web, CLI, TUI, and MCP adapters."""

from typing import Annotated

from django.db.models import CharField, F, IntegerField, QuerySet, Value
from django.db.models.functions import Cast
from pydantic import Field

from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.search_index.backends import SnippetParts, search_backend
from hq.platform.search_index.registry import by_scope

from .integration_specs import ListRead, ResourceSpec
from .projection import MAX_PAGE_SIZE, projection_scope
from .resources import TopQuery, resource_search_capabilities
from .security import Capability, Principal
lazy from . import derived_reads
lazy from .search_records import Match

MAX_SEARCH_RESULTS = 5000


class UnknownSearchScope(ValueError):
    pass


def _authorize(scope: str, principal: Principal) -> None:
    if scope not in by_scope():
        raise UnknownSearchScope(f"Unknown search scope {scope!r}.")
    principal.require(*_required(scope))


def _required(scope: str) -> tuple[Capability | str, ...]:
    # Every scope is readable with the baseline READ capability except the audit
    # trail, a security log, which its resource gates behind its own.
    return resource_search_capabilities().get(scope, (Capability.READ,))


def apply_search(queryset: QuerySet, *, scope: str, query: str, principal: Principal) -> QuerySet:
    """Filter a domain queryset while retaining relevance as its default order.

    ``_search_rank`` is the row's position among the index's ranked hits, the
    same order ``global_search`` returns, so ordering by it
    is total and a page boundary never moves. The index states it in SQL: the
    statement carries the same few parameters however many rows match.
    """
    _authorize(scope, principal)
    identifier_field = by_scope()[scope].identifier_field
    position = search_backend.position(
        scope=scope,
        query=query,
        limit=MAX_SEARCH_RESULTS,
        identifier=Cast(F(identifier_field), CharField()),
    )
    if position is None:
        return _no_matches(queryset)
    return queryset.annotate(_search_rank=position).filter(_search_rank__isnull=False)


def _no_matches(queryset: QuerySet) -> QuerySet:
    """An empty result that can still be ordered by relevance.

    ``order_by("_search_rank")`` resolves the name against the model whether
    or not a row is built, so the annotation is part of what an empty result
    promises.
    """
    return queryset.none().annotate(_search_rank=Value(0, output_field=IntegerField()))


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
        else definition.model.objects.in_bulk(typed_ids, field_name=definition.identifier_field)
    )
    return {
        object_id: records[typed_id]
        for object_id, typed_id in zip(object_ids, typed_ids, strict=True)
        if typed_id in records
    }


def global_search(query: str, *, principal: Principal, limit_per_scope: int = 8) -> dict:
    """Relevance-ranked, snippeted results across every scope the principal
    may search. Scopes the principal lacks (e.g. the audit log for a
    least-privilege adapter) are omitted entirely, not shown empty."""

    with projection_scope():
        return _global_search(query, principal=principal, limit_per_scope=limit_per_scope)


def _may_search(scope: str, principal: Principal) -> bool:
    return principal.permits(*_required(scope))


def _global_search(query: str, *, principal: Principal, limit_per_scope: int) -> dict:
    # Authorization decides which scopes the index is asked about, so a scope
    # the principal may not search is never read, not read and then dropped.
    definitions = by_scope()
    permitted = [scope for scope in definitions if _may_search(scope, principal)]
    hits_by_scope = search_backend.search_scopes(scopes=permitted, query=query, limit=limit_per_scope)
    groups = []
    for scope in permitted:
        definition = definitions[scope]
        items = _global_items(definition, hits_by_scope.get(scope, []))
        groups.append(
            {
                "scope": scope,
                "label": definition.label,
                "count": len(items),
                "items": items,
            }
        )
    return {
        "query": query,
        "total": sum(group["count"] for group in groups),
        "groups": groups,
    }


def _global_items(
    definition: SearchDefinition,
    hits: list[tuple[str, SnippetParts]],
) -> list[dict]:
    if not hits:
        return []
    records = _fetch_records(definition, [object_id for object_id, _ in hits])
    return [
        {
            "id": object_id,
            "title": definition.title(record),
            "badge": definition.badge(record),
            "url": definition.url(record),
            "timestamp": definition.timestamp(record),
            "snippet": snippet,
        }
        for object_id, snippet in hits
        if (record := records.get(object_id)) is not None and definition.listed(record)
    ]


# Search's, which answers fewer unless asked for more.
type SearchLimit = Annotated[
    int, Field(ge=1, le=MAX_PAGE_SIZE, description=f"How many matches to return, 1 to {MAX_PAGE_SIZE}, best first.")
]


# Search ranks each scope apart and cuts each before merging them, so no one
# order holds a place to resume from: a caller narrows the query instead.
class SearchQuery(TopQuery):
    query: str = Field(
        default="",
        max_length=200,
        description="What to look for in names and records' text; blank finds nothing.",
    )
    limit: SearchLimit = 20


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "search",
            "Search",
            "Machines, services and domains, then records, matching a query.",
            Capability.READ,
            listing=ListRead(derived_reads.search, SearchQuery, Match, counted=False),
            web_route="search",
            pass_principal=True,
        ),
    )
