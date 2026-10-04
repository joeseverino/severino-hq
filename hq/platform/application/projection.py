"""Primitives every read projection needs, declared once.

Small on purpose, and deliberately dependency-free: this is imported by modules
that serialize projects, assets, content, expenses and infrastructure, and a
shared module that reached for any of their models would make importing one of
them mean importing all of them.

What lives here is the handful of rules every surface shares: paging bounds,
the page-size ceiling and the timestamp rendering. Each is defined once so a
change to it reaches every surface.
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


def day_span(first, last=None):
    """A run of local days as the moments a datetime column is filtered by.

    Returns ``(start, end)``: the first day's midnight and the midnight after
    the last, in HQ's timezone, for ``column__gte=start, column__lt=end``.
    ``last`` left out means from the first day on, and ``end`` is None.

    Asked this way the database compares the column as stored and can use its
    index. Asked as ``column__date__gte=day`` it has to turn every row's moment
    into a local date first, which on SQLite is a call back into Python per
    row: the slow part of a read that returns a few hundred rows of thousands.
    """

    from datetime import date as _date, datetime, time, timedelta

    from django.utils import timezone

    def day(value):
        if isinstance(value, datetime):
            return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
        if not isinstance(value, _date):
            raise TypeError("day_span takes dates")
        return value

    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day(first), time.min), zone)
    if last is None:
        return start, None
    return start, timezone.make_aware(datetime.combine(day(last) + timedelta(days=1), time.min), zone)


def years_of(model, field: str) -> list[int]:
    """Each calendar year a date column holds a row for, oldest first.

    The distinct days come off the column as stored. ``dates(field, "year")``
    truncates every row first, which on SQLite is a call back into Python per
    row to answer with a handful of years.
    """

    days = (
        model.objects.exclude(**{field: None}).order_by().values_list(field, flat=True).distinct()
    )
    return sorted({day.year for day in days})


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

    Domains differ only in the model, the serializer and which fields a search
    looks at: a person searches an asset by vendor and a project by the
    technologies it uses. Nothing else about the read differs.
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
    catch that one by name, and because this module stays free of domain
    imports on purpose, per the note at the top.
    """

    try:
        row = model.objects.get(slug=slug)
    except model.DoesNotExist as exc:
        raise missing(f"{label} {slug!r} was not found.") from exc
    return serialize(row, relationships=True)
