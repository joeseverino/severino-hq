"""Primitives every read projection needs, declared once.

Small on purpose, and deliberately dependency-free: this is imported by modules
that serialize projects, assets, content, expenses and infrastructure, and a
shared module that reached for any of their models would make importing one of
them mean importing all of them.

What lives here is the handful of rules that were copied instead. Paging bounds
had four spellings and the ceiling itself had three definitions plus one inline
literal, which is how a limit gets raised in one place and quietly not in the
others. A timestamp had four identical renderers, which is one per surface that
would have to be found on the day an API is asked for something other than ISO.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Mapping, TypeVar

from django.db.models import Q

# The most rows any single read will return, whatever a caller asks for.
#
# A ceiling rather than a suggestion: these projections are reached from the
# web, the API and the MCP, and the last of those is driven by a model that will
# cheerfully ask for a million records and then try to read them.
MAX_PAGE_SIZE = 100

_T = TypeVar("_T")
_MISSING = object()
_READ_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "hq_projection_read_scope", default=None
)


@contextmanager
def projection_scope(seed: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Share exact read results for one assembled projection, then forget them.

    This is request/use-case memoisation, not a process cache. Nested composers
    reuse the active scope and the outermost caller clears it in ``finally``,
    so two consumers assembling one answer cannot disagree or repeat a read,
    while the next request always sees current state.
    """

    current = _READ_SCOPE.get()
    if current is not None:
        if seed:
            current.update(seed)
        yield
        return
    token = _READ_SCOPE.set(dict(seed or {}))
    try:
        yield
    finally:
        _READ_SCOPE.reset(token)


def read_once(key: str, loader: Callable[[], _T]) -> _T:
    """Load once inside ``projection_scope``; behave normally outside one."""

    scope = _READ_SCOPE.get()
    if scope is None:
        return loader()
    found = scope.get(key, _MISSING)
    if found is _MISSING:
        found = loader()
        scope[key] = found
    return found


def page_size(limit: int, *, maximum: int = MAX_PAGE_SIZE) -> int:
    """How many rows to actually return for a requested limit.

    Rejects nonsense rather than silently correcting it: a caller asking for
    zero or a negative page has a bug, and quietly handing back the default
    hides it behind a page that looks fine.
    """

    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, maximum)


def iso(value: Any) -> str | None:
    """A timestamp as a contract renders it, or None when there is none.

    None rather than an empty string, because these become JSON: a consumer can
    test for null, while "" is a value that has to be special-cased at every
    call site to mean absent.
    """

    return value.isoformat() if value else None


def listing(model, serialize, *, search: tuple[str, ...], status=None, query=None,
            limit: int = 50) -> dict[str, Any]:
    """One list read: an optional status, an optional text match, one page.

    Written out per domain this was four functions differing only in the model,
    the serializer and which fields a search looks at -- and the graph flagged
    every pair. The fields differ because a person searches an asset by vendor
    and a project by the technologies it uses; nothing else about the read does.
    """

    qs = model.objects.all()
    if status:
        qs = qs.filter(status=status)
    if query:
        matches = Q()
        for field in search:
            matches |= Q(**{f"{field}__icontains": query})
        qs = qs.filter(matches)
    items = [row for row in qs.order_by("slug")[: page_size(limit)]]
    return {"items": [serialize(row) for row in items], "count": len(items)}


def addressable(model, serialize, slug: str, *, label: str, missing) -> dict[str, Any]:
    """One record by the slug every registered resource is addressed with.

    ``label`` names the thing in the error, because "Asset 'x' was not found"
    is the sentence a client shows and the model's own name is not always it.

    ``missing`` is the exception class to raise. Passed in rather than imported
    because each domain declares its own ``NotFoundError`` and its callers
    catch that one by name -- and because this module stays free of domain
    imports on purpose, per the note at the top.
    """

    try:
        row = model.objects.get(slug=slug)
    except model.DoesNotExist as exc:
        raise missing(f"{label} {slug!r} was not found.") from exc
    return serialize(row, relationships=True)
