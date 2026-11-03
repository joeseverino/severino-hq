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
from typing import Any, TypedDict

from django.conf import settings
from django.db.models import FETCH_PEERS, FETCH_RAISE, Q, QuerySet
lazy from django.utils import timezone

from .record_errors import RecordNotFound

# The most rows any single read will return, whatever a caller asks for.
#
# A ceiling rather than a suggestion: these projections are reached from the
# web, the API and the MCP, and the last of those is driven by a model that will
# cheerfully ask for a million records and then try to read them.
MAX_PAGE_SIZE = 100


class NextPage(TypedDict):
    """Where the page after a list's is read from: ``cursor``, sent back with the same filters."""

    cursor: str


def shape_version(version: int) -> dict[str, Any]:
    """A projection's ``schema_version`` as JSON Schema: the one number its answer carries."""

    return {"type": "integer", "const": version}


_MISSING = object()
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


def iso(value: Any) -> str | None:
    """A timestamp as a contract renders it, or None when there is none.

    None rather than an empty string, because these become JSON: a consumer can
    test for null, while "" is a value that has to be special-cased at every
    call site to mean absent.
    """

    return value.isoformat() if value else None


def guarded(queryset: QuerySet) -> QuerySet:
    """A list read's queryset, with a fetch its rows did not plan for caught.

    A related or deferred field read from a row that did not fetch it costs a
    query per row. Under test and in development it raises ``FieldFetchBlocked``,
    so the first test that renders the list finds the missing ``select_related``;
    in production it is fetched once for the whole page. A reverse foreign key or
    many-to-many manager is beyond Django's fetch modes; a query budget holds it.
    """

    return queryset.fetch_mode(FETCH_RAISE if settings.SEVERINO_STRICT_FETCH else FETCH_PEERS)


def listing(
    rows,
    serialize,
    *,
    search: tuple[str, ...] = (),
    status=None,
    query=None,
    order: tuple[str, ...] = ("slug",),
    limit: int = 50,
) -> dict[str, Any]:
    """One list read: an optional status, an optional text match, one page.

    Domains differ only in the rows (a model, or a queryset a domain has
    already narrowed or joined), the serializer, the order and which fields a
    search looks at: a person searches an asset by vendor and a project by the
    technologies it uses. Each item is the serializer's, so a record's list
    item is the same shape as the record a save or a fetch answers.
    """

    qs = guarded(rows if isinstance(rows, QuerySet) else rows.objects.all())
    if status:
        qs = qs.filter(status=status)
    if query:
        matches = Q()
        for field in search:
            matches |= Q(**{f"{field}__icontains": query})
        qs = qs.filter(matches)
    page = keyset_page(qs, order, limit)
    return {**page, "items": [serialize(row) for row in page["items"]]}


def complete(items: list[Any]) -> dict[str, Any]:
    """A collection holding every match, in the fields every list read answers with.

    For a projection a page and a read both draw: the page renders the same
    dict the read returns, so neither adds paging fields the other lacks.
    """

    return {"items": items, "count": len(items), "total": len(items), "truncated": False, "next": None}


def matched(rows, page: list[Any], limit: int) -> int:
    """How many rows the page was cut from: counted only when the page is full."""

    return len(page) if len(page) < page_size(limit) else rows.count()


# ----- Paging -------------------------------------------------------------------
#
# A list read past its first page sends back the cursor the page before it
# answered with. ``application.resources`` signs it and binds it to the kind and
# filters it was issued under; what it carries is a position only the cut that
# wrote it reads: a keyset over a queryset's order, or a place in a list derived
# whole on every read. The read opens ``paging`` around its handler, and the
# first cut made inside it resumes from the position and says where it stopped,
# so no handler passes a cursor along.


class CursorRefusal(QueryRefusal):
    """A position no cut made under these terms could have written."""


@dataclass(slots=True)
class PageScope:
    """One list read's position: where the page before it stopped, and, once a cut
    claims it, where this page stops (``None`` past the last row)."""

    position: dict[str, Any] | None
    claimed: bool = False
    after: dict[str, Any] | None = None


_PAGE: ContextVar[PageScope | None] = ContextVar("hq_projection_page", default=None)


@contextmanager
def paging(position: dict[str, Any] | None) -> Iterator[PageScope]:
    """The scope a list read's handler runs in, resuming from ``position``."""

    scope = PageScope(position)
    with _PAGE.set(scope):
        yield scope


def _claimed() -> PageScope | None:
    """The list read's scope for the first cut made inside it; nothing for any later
    cut, which is a page of its own inside one item."""

    scope = _PAGE.get()
    if scope is None or scope.claimed:
        return None
    scope.claimed = True
    return scope


def _keyset(qs: QuerySet, order: tuple[str, ...]) -> tuple[tuple[str, bool], ...]:
    """``order`` as the column names a keyset compares and whether each descends,
    with the primary key last so no two rows tie."""

    keys = tuple((name.removeprefix("-"), name.startswith("-")) for name in order)
    if not any(name in ("pk", qs.model._meta.pk.name) for name, _ in keys):
        keys += (("pk", keys[-1][1] if keys else False),)
    for name, _ in keys:
        field = qs.model._meta.pk if name == "pk" else qs.model._meta.get_field(name)
        if field.null:
            # NULL sorts apart from every value and compares to none, so a keyset over it skips rows.
            raise TypeError(f"{qs.model.__name__}.{name} is nullable, so a list cannot page by it.")
    return keys


def _cursor_value(value: Any) -> Any:
    return (
        value.isoformat()
        if isinstance(value, date | datetime)
        else value
        if isinstance(value, int | str)
        else str(value)
    )


def _past(keys: tuple[tuple[str, bool], ...], position: dict[str, Any]) -> Q:
    """The rows after ``position`` in the keyset ``keys``: past it on the first key,
    or tied on each earlier key and past it on the next."""

    values = position.get("keys")
    if position.get("order") != [name for name, _ in keys] or not isinstance(values, list) or len(values) != len(keys):
        raise CursorRefusal("cursor was issued for another order of this list.")
    after = Q()
    for index, (name, descending) in enumerate(keys):
        step = Q(**{f"{name}__{'lt' if descending else 'gt'}": values[index]})
        for earlier, (prior, _) in enumerate(keys[:index]):
            step &= Q(**{prior: values[earlier]})
        after |= step
    return after


def keyset_page(qs: QuerySet, order: tuple[str, ...], limit: int) -> dict[str, Any]:
    """One page of ``qs`` in ``order``: past the list read's cursor when it sent one,
    and with the position of its last row when more follow."""

    keys = _keyset(qs, order)
    names = [name for name, _ in keys]
    scope, size = _claimed(), page_size(limit)
    position = scope.position if scope is not None else None
    cut = qs.filter(_past(keys, position)) if position is not None else qs
    rows = list(cut.order_by(*(f"-{name}" if descending else name for name, descending in keys))[: size + 1])
    page, more = rows[:size], len(rows) > size
    total = qs.count() if more or position is not None else len(page)
    found: dict[str, Any] = {"items": page, "count": len(page), "total": total}
    if scope is not None:
        last = page[-1] if more else None
        scope.after = (
            None
            if last is None
            else {"order": names, "keys": [_cursor_value(last.serializable_value(n)) for n in names]}
        )
        found["truncated"] = more
    return found


def window[T](items: Sequence[T], limit: int, *, maximum: int = MAX_PAGE_SIZE) -> tuple[Sequence[T], dict[str, Any]]:
    """One page of a list derived whole on every read, and its paging fields;
    ``maximum`` is a page's ceiling where its list declares its own.

    The position is a place in the list: its order is the derivation's (worst
    first, say), which moves as the estate does, so no key holds a row's place,
    and a row that moved between two reads can be skipped or shown twice.
    """

    scope, size = _claimed(), page_size(limit, maximum=maximum)
    start = 0
    if scope is not None and scope.position is not None:
        at: object = scope.position.get("at")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise CursorRefusal("cursor was issued for another order of this list.")
        start = at
    page = items[start : start + size]
    more = start + size < len(items)
    found: dict[str, Any] = {"count": len(page), "total": len(items)}
    if scope is not None:
        scope.after = {"at": start + size} if more else None
        found["truncated"] = more
    return page, found


def addressable(model, serialize, slug: str) -> dict[str, Any]:
    """One record by the slug every registered resource is addressed with."""

    try:
        row = model.objects.get(slug=slug)
    except model.DoesNotExist as exc:
        raise RecordNotFound("No record has that slug.") from exc
    return serialize(row, relationships=True)
