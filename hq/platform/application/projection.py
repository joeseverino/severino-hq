"""Primitives every read projection needs, declared once.

Dependency-free on purpose: modules serializing projects, assets, content,
expenses and infrastructure import it, and a shared module that reached for
their models would make importing one mean importing all. The rules every
surface shares (paging bounds, the page-size ceiling, timestamp rendering, the
refusal of an input that cannot narrow) are each defined once here.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, overload

from django.conf import settings
from django.db.models import FETCH_PEERS, FETCH_RAISE, QuerySet
from pydantic import ConfigDict
lazy from django.utils import timezone

from .record_errors import RecordNotFound
lazy from .search import apply_search
lazy from .security import Principal

# The most rows any single read will return, whatever a caller asks for.
#
# A ceiling rather than a suggestion: these projections are reached from the
# web, the API and the MCP, and the last of those is driven by a model that will
# cheerfully ask for a million records and then try to read them.
MAX_PAGE_SIZE = 100


_MISSING = sentinel("_MISSING")
_READ_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar("hq_projection_read_scope", default=None)
# Where a scope keeps what it was seeded with, apart from what it read since.
_SEED = "projection.seed"


class QueryRefusal(ValueError):
    """A narrowing input a projection cannot be read by.

    ``reason`` is HQ's own sentence for the caller, and the only text an
    adapter answers with: never ``str(exc)``, never the value sent.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# A read's query model, which every door that serves the read publishes and
# validates by: an input the read does not take, or one not of its type, is
# refused rather than dropped or coerced, and the refusal never quotes it.
QUERY = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, frozen=True)


@contextmanager
def projection_scope(seed: Mapping[str, Any] | None = None, *, apart: bool = False) -> Iterator[None]:
    """Share exact read results for one assembled projection, then forget them.

    Request and use-case memoization, never a process cache: two consumers
    assembling one answer cannot disagree or repeat a read, and the next request
    sees current state. ``apart`` opens a scope of its own, starting from
    ``seed`` alone, for asking a question again as it was first asked.
    """

    # Nested composers reuse the active scope; the outermost caller clears it.
    current = _READ_SCOPE.get()
    if current is not None and not apart:
        if seed:
            current.update(seed)
            current[_SEED] = {**current.get(_SEED, {}), **seed}
        yield
        return
    with _READ_SCOPE.set({**(seed or {}), _SEED: dict(seed or {})}):
        yield


def seeded() -> dict[str, Any]:
    """What the projection in progress was seeded with; nothing outside one."""

    scope = _READ_SCOPE.get()
    return dict(scope.get(_SEED, {})) if scope is not None else {}


def read_once[T](key: str, loader: Callable[[], T]) -> T:
    """Load once inside ``projection_scope``; behave normally outside one."""

    scope = _READ_SCOPE.get()
    if scope is None:
        return loader()
    found = scope.get(key, _MISSING)
    if found is _MISSING:
        found = loader()
        scope[key] = found
    return found


def day_span(first: date, last: date | None = None) -> tuple[datetime, datetime | None]:
    """A run of local days as the moments a datetime column is filtered by.

    Returns ``(start, end)``: the first day's midnight and the midnight after
    the last, in HQ's timezone, for ``column__gte=start, column__lt=end``; with
    ``last`` left out, from the first day on and ``end`` None. Compared as
    stored, the column can use its index, where ``column__date__gte=day`` makes
    SQLite call back into Python for every row.
    """

    def day(value: date) -> date:
        if isinstance(value, datetime):
            return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
        if not isinstance(value, date):
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

    days = model.objects.exclude(**{field: None}).order_by().values_list(field, flat=True).distinct()
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


@overload
def iso(value: date, *, timespec: str = ...) -> str: ...
@overload
def iso(value: date | None, *, timespec: str = ...) -> str | None: ...
def iso(value: date | None, *, timespec: str = "auto") -> str | None:
    """A day or instant as a contract renders it, or None when there is none.

    None rather than an empty string, because these become JSON: a consumer can
    test for null, while "" is a value that has to be special-cased at every
    call site to mean absent. ``timespec`` is ``datetime.isoformat``'s, for an
    instant a reader needs only to the second or minute.
    """

    if not value:
        return None
    return value.isoformat(timespec=timespec) if isinstance(value, datetime) else value.isoformat()


def guarded(queryset: QuerySet) -> QuerySet:
    """A list read's queryset, with a fetch its rows did not plan for caught.

    A related or deferred field read from a row that did not fetch it costs a
    query per row. Under test and in development it raises ``FieldFetchBlocked``,
    so the first test that renders the list finds the missing ``select_related``;
    in production it is fetched once for the whole page. A reverse foreign key or
    many-to-many manager is beyond Django's fetch modes; a query budget holds it.
    """

    return queryset.fetch_mode(FETCH_RAISE if settings.SEVERINO_STRICT_FETCH else FETCH_PEERS)


def listing(rows, serialize, *, status=None, order: tuple[str, ...] = ("slug",), limit: int = 50) -> dict[str, Any]:
    """One list read: an optional status, one page.

    Domains differ only in the rows (a model, or a queryset a domain has
    already narrowed or joined), the serializer and the order. Each item is
    the serializer's, so a record's list item is the same shape as the record
    a save or a fetch answers.
    """

    qs = guarded(rows if isinstance(rows, QuerySet) else rows.objects.all())
    if status:
        qs = qs.filter(status=status)
    page = page_of(qs, limit, order=order)
    return {**page, "items": [serialize(row) for row in page["items"]]}


def complete(items: list[Any]) -> dict[str, Any]:
    """A collection holding every match, in the fields every list read answers with.

    For a projection a page and a read both draw: the page renders the same
    dict the read returns, so neither adds paging fields the other lacks.
    """

    return {
        "items": items,
        "count": len(items),
        "total": len(items),
        "truncated": False,
        "next": None,
        "previous": None,
    }


def matched(rows, page: list[Any], limit: int) -> int:
    """How many rows the page was cut from: counted only when the page is full."""

    return len(page) if len(page) < page_size(limit) else rows.count()


# ----- Paging -------------------------------------------------------------------
#
# A list read is asked for a page by its number, 1 first, as GitHub's and
# GitLab's sortable lists are: a number holds under any order (a nullable
# column, an annotation, a search's rank), and an address that names one is
# bookmarked. A row that moves between two reads can be skipped or shown
# twice, which a single-writer list of thousands of rows accepts. The read
# opens ``paging`` around its handler with the page, the search and the order
# its caller asked for, and the first cut made inside it searches, orders and
# cuts the page and says whether more follow, so no handler passes a search, a
# sort or a page along.


@dataclass(frozen=True, slots=True)
class Search:
    """A list read's search: the text, the scope it is looked up in, and who asks."""

    scope: str
    text: str
    principal: Principal


@dataclass(slots=True)
class PageScope:
    """One list read's page, its search, the order its caller asked for and the
    one its list declares first, whether it counts its rows, and, once a cut
    claims it, whether more follow."""

    page: int = 1
    search: Search | None = None
    order: tuple[str, ...] = ()
    default: tuple[str, ...] = ()
    counted: bool = True
    claimed: bool = False
    more: bool = False


_PAGE: ContextVar[PageScope | None] = ContextVar("hq_projection_page", default=None)


@contextmanager
def paging(scope: PageScope) -> Iterator[PageScope]:
    """The scope a list read's handler runs in."""

    with _PAGE.set(scope):
        yield scope


def _claimed() -> PageScope:
    """The list read's scope for the first cut made inside it; the first page
    of all for any later cut, which is a page of its own inside one item, and
    for a cut made outside a read."""

    scope = _PAGE.get()
    if scope is None or scope.claimed:
        return PageScope(claimed=True)
    scope.claimed = True
    return scope


def _ordered(rows: QuerySet, order: tuple[str, ...]) -> QuerySet:
    """``rows`` in ``order``, with the primary key last so no two rows tie and
    a page holds the same rows on every read."""

    keys = tuple(name.removeprefix("-") for name in order)
    if not any(key in ("pk", rows.model._meta.pk.name) for key in keys):
        order = (*order, "pk")
    return rows.order_by(*order)


def page_of(
    rows: QuerySet | Sequence[Any], limit: int, *, order: tuple[str, ...] = (), maximum: int = MAX_PAGE_SIZE
) -> dict[str, Any]:
    """One page of a list read: a queryset, the rows its search finds, in the
    order its caller asked for, else the best match first, else the one its
    list declares first, else ``order``; or a list derived whole on every
    read, in its own order. ``maximum`` is a page's ceiling where its list
    declares its own; an uncounted list reads one row past its page to learn
    whether more follow, and counts nothing."""

    scope, size = _claimed(), page_size(limit, maximum=maximum)
    start = (scope.page - 1) * size
    total: int | None
    if isinstance(rows, QuerySet):
        if scope.search is not None:
            rows = apply_search(
                rows, scope=scope.search.scope, query=scope.search.text, principal=scope.search.principal
            )
        rows = _ordered(rows, scope.order or (("_search_rank",) if scope.search else scope.default or order))
        cut = list(rows[start : start + size + 1])
        page, more = cut[:size], len(cut) > size
        total = start + len(page) if not more and (page or not start) else rows.count() if scope.counted else None
    else:
        if scope.search is not None or scope.order:
            raise RuntimeError("A list derived whole has its own search and order, so its query declares neither.")
        page, more, total = list(rows[start : start + size]), start + size < len(rows), len(rows)
    scope.more = more
    return {"items": page, "count": len(page), "total": total, "truncated": more}


def addressable(model, serialize, slug: str) -> dict[str, Any]:
    """One record by the slug every registered resource is addressed with."""

    try:
        row = model.objects.get(slug=slug)
    except model.DoesNotExist as exc:
        raise RecordNotFound("No record has that slug.") from exc
    return serialize(row, relationships=True)
