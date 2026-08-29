"""What the site earned, and which published thing earned it.

The controller reads Cloudflare and posts what it found; this records it and
answers questions about it. Two halves, and the split matters: recording is an
observation arriving from outside, reading is every surface in HQ, and neither
knows how the other works.

One join runs through the whole module. Cloudflare reports a request path; HQ
already knows what it publishes and where. Matching those is what turns "this
path was requested 412 times" into "that writeup was read 412 times", and it is
the only reason this is more useful than the dashboard it reads from.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from analytics.models import AnalyticsSite, RumDaily, VitalsDaily
from content.models import PAGE_TYPES, WRITEUP_TYPES, ContentItem

from .cadence import sweep_interval
from .security import Capability, Principal

# The window the analytics page opens on. A day, because the question that
# brings someone to this page is "what is happening", and a four-week average is
# the wrong shape for it -- yesterday's spike disappears into it entirely.
DEFAULT_WINDOW_DAYS = 1

# What a content section means by "views". Deliberately not the above: a
# writeup published last spring earns its traffic over months, and a day's
# figure beside it would say almost every writeup is worth nothing.
CONTENT_TRAFFIC_DAYS = 28


def location_of(url: str) -> tuple[str, str]:
    """The measured host and path for one published URL.

    A path is only unique inside a host. Keeping both parts here prevents a
    writeup at ``example.com/about/`` inheriting traffic from another measured
    site that happens to publish ``/about/`` too.
    """

    if not url:
        return "", ""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").rstrip(".").lower()
    return host, parsed.path or "/"


def path_of(url: str) -> str:
    """The request path a published URL would be requested as.

    Cloudflare reports what the browser asked for, so this keeps the trailing
    slash rather than tidying it away: the site serves one form and redirects
    the other, and a normalisation here would silently match a path that never
    gets requested. Location parsing lives in one place; path-only consumers
    derive their view from it.
    """

    return location_of(url)[1]


# ----- Recording -------------------------------------------------------------


@transaction.atomic
def record_analytics(
    payload: dict[str, Any], *, principal: Principal, controller_id: str = ""
) -> dict[str, Any]:
    """Store one controller's reading of every site it can see.

    Accumulates rather than replaces, which is the one way this differs from
    every other controller report. A connection HQ can no longer reach should
    stop being listed; a day of traffic that already happened should not stop
    having happened because the window moved past it. Only days inside the
    reported window are touched.

    Idempotent by construction: the grain is unique, so re-running a sweep --
    or replaying an older one -- restates a day rather than doubling it.
    """

    # The same capability every controller report carries. Analytics arrives by
    # the same road, from the same principal, and inventing a second capability
    # for it would mean two answers to "may this controller report".
    principal.require(Capability.MANAGE_INFRASTRUCTURE)

    sites = payload.get("sites") or []
    recorded = {"sites": 0, "rows": 0, "vitals": 0}
    for entry in sites:
        site_tag = str(entry.get("site_tag", "")).strip()
        host = str(entry.get("host", "")).strip().rstrip(".").lower()
        connection_ref = str(entry.get("connection_ref", ""))[:100]
        if not site_tag or not host:
            continue
        site, _ = AnalyticsSite.objects.update_or_create(
            connection_ref=connection_ref,
            site_tag=site_tag,
            defaults={
                "host": host[:255],
            },
        )
        recorded["sites"] += 1

        for row in entry.get("rows") or []:
            day = _as_date(row.get("date"))
            dimension = str(row.get("dimension", ""))
            value = str(row.get("value", "")).strip()
            if not day or not value or dimension not in RumDaily.Dimension.values:
                continue
            RumDaily.objects.update_or_create(
                site=site,
                date=day,
                dimension=dimension,
                value=value[:512],
                defaults={
                    "pageviews": max(int(row.get("pageviews") or 0), 0),
                    "visits": max(int(row.get("visits") or 0), 0),
                    "sample_interval": max(int(row.get("sample_interval") or 1), 1),
                },
            )
            recorded["rows"] += 1

        for reading in entry.get("vitals") or []:
            day = _as_date(reading.get("date"))
            if not day:
                continue
            VitalsDaily.objects.update_or_create(
                site=site,
                date=day,
                defaults={
                    column: reading.get(column)
                    for column in (
                        "largest_contentful_paint_ms",
                        "interaction_to_next_paint_ms",
                        "first_contentful_paint_ms",
                        "time_to_first_byte_ms",
                        "cumulative_layout_shift",
                    )
                }
                | {
                    column: max(int(reading.get(column) or 0), 0)
                    for column in (
                        "lcp_good",
                        "lcp_needs_improvement",
                        "lcp_poor",
                        "inp_good",
                        "inp_needs_improvement",
                        "inp_poor",
                        "cls_good",
                        "cls_needs_improvement",
                        "cls_poor",
                    )
                }
                | {"sample_interval": max(int(reading.get("sample_interval") or 1), 1)},
            )
            recorded["vitals"] += 1

    return {"ok": True, "recorded": recorded, "observed_at": timezone.now().isoformat()}


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# ----- Reading ---------------------------------------------------------------


def _window(days: int) -> tuple[date, date]:
    """The window ending today, inclusive.

    Today is included even though it is still accumulating. A partial day
    beside complete ones would misread as a collapse, which is why the reader
    used to stop at yesterday -- but the sweep re-reads a rolling window and
    restates each day as it fills, so today is simply the most recent reading
    rather than a permanent undercount. Excluding it would mean the shortest
    window on the page could not answer what is happening now.
    """

    end = timezone.now().date()
    return end - timedelta(days=max(days, 1) - 1), end


def breakdown(
    dimension: str, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 50
) -> list[dict[str, Any]]:
    """One breakdown over a window, biggest first.

    Summed across days rather than read per day, because a window is the
    question: "which pages did well this month" is not answered by twenty-eight
    separate answers to "which pages did well on the 3rd".
    """

    start, end = _window(days)
    rows = (
        RumDaily.objects.filter(dimension=dimension, date__gte=start, date__lte=end)
        .values("value")
        .annotate(pageviews=Sum("pageviews"), visits=Sum("visits"))
        .order_by("-pageviews", "value")[:limit]
    )
    return [dict(row) for row in rows]


def _traffic_for_locations(
    locations: set[tuple[str, str]], *, days: int = DEFAULT_WINDOW_DAYS
) -> dict[tuple[str, str], dict[str, int]]:
    """Traffic for exact published locations in one bounded query.

    The caller supplies the locations it can render. That keeps attribution at
    its real ``(host, path)`` grain and avoids loading an estate's entire path
    history merely to annotate one page of content.
    """

    locations = {(host.lower(), path) for host, path in locations if host and path}
    if not locations:
        return {}
    hosts = {host for host, _ in locations}
    paths = {path for _, path in locations}
    start, end = _window(days)
    rows = (
        RumDaily.objects.filter(
            site__host__in=hosts,
            dimension=RumDaily.Dimension.PATH,
            value__in=paths,
            date__gte=start,
            date__lte=end,
        )
        .values("site__host", "value")
        .annotate(pageviews=Sum("pageviews"), visits=Sum("visits"))
    )
    return {
        key: {"pageviews": row["pageviews"], "visits": row["visits"]}
        for row in rows
        if (key := (row["site__host"].lower(), row["value"])) in locations
    }


def measured_path_count(*, days: int = DEFAULT_WINDOW_DAYS) -> int:
    """How many site-local paths were measured in the window."""

    start, end = _window(days)
    return (
        RumDaily.objects.filter(
            dimension=RumDaily.Dimension.PATH, date__gte=start, date__lte=end
        )
        .values("site_id", "value")
        .distinct()
        .count()
    )


def published_traffic(
    content_types: frozenset[str], *, days: int = CONTENT_TRAFFIC_DAYS
) -> list[dict[str, Any]]:
    """Published items of one half of the registry, with what each earned.

    Items with no traffic are kept and sorted last. A writeup nobody read is
    the most actionable row on the page, and dropping it because it has no
    number would hide exactly the thing worth seeing.
    """

    items = list(
        ContentItem.objects.filter(
            content_type__in=content_types, status=ContentItem.Status.PUBLISHED
        ).only("title", "slug", "content_type", "published_url", "published_at")
    )
    locations = {location_of(item.published_url) for item in items}
    traffic = _traffic_for_locations(locations, days=days)

    rows = []
    for item in items:
        location = location_of(item.published_url)
        path = location[1]
        measured = traffic.get(location, {})
        rows.append(
            {
                "item": item,
                "path": path,
                "pageviews": measured.get("pageviews", 0),
                "visits": measured.get("visits", 0),
                "measured": bool(measured),
            }
        )
    rows.sort(key=lambda row: (-row["pageviews"], row["item"].title))
    return rows


def attach_traffic(items, *, days: int = CONTENT_TRAFFIC_DAYS):
    """Give each content item what its published URL earned.

    Annotates in place rather than returning rows, so a list already paginated
    and sorted by the table engine keeps both. The join is one query for the
    whole page regardless of how many items are on it -- the alternative,
    asking per row, is the N+1 the frontend bar exists to prevent.

    ``pageviews`` is None where nothing was measured, which a template can tell
    from a real zero: one means nobody visited, the other means nobody looked.
    """

    items = list(items)
    if not items:
        return items
    locations = {location_of(getattr(item, "published_url", "")) for item in items}
    traffic = _traffic_for_locations(locations, days=days)
    for item in items:
        measured = traffic.get(location_of(getattr(item, "published_url", "")))
        item.pageviews = measured["pageviews"] if measured else None
        item.visits = measured["visits"] if measured else None
    return items


def item_traffic(item, *, days: int = CONTENT_TRAFFIC_DAYS) -> dict[str, Any]:
    """One published item's traffic, and its recent days for a trend.

    Answers the question the detail page is actually for: not "how did the site
    do" but "did anyone read this". Returns ``measured: False`` rather than
    zeros when the path was never seen, because an item published yesterday and
    an item nobody opens are different facts and a 0 says the wrong one.
    """

    host, path = location_of(getattr(item, "published_url", ""))
    if not host or not path:
        return {
            "path": "",
            "measured": False,
            "pageviews": 0,
            "visits": 0,
            "days": days,
        }

    start, end = _window(days)
    rows = list(
        RumDaily.objects.filter(
            dimension=RumDaily.Dimension.PATH,
            site__host=host,
            value=path,
            date__gte=start,
            date__lte=end,
        )
        .values("date")
        .annotate(pageviews=Sum("pageviews"), visits=Sum("visits"))
        .order_by("date")
    )
    return {
        "path": path,
        "measured": bool(rows),
        "pageviews": sum(row["pageviews"] for row in rows),
        "visits": sum(row["visits"] for row in rows),
        "days": days,
        "series": rows,
    }


def writeup_traffic(*, days: int = CONTENT_TRAFFIC_DAYS) -> list[dict[str, Any]]:
    return published_traffic(WRITEUP_TYPES, days=days)


def page_traffic(*, days: int = CONTENT_TRAFFIC_DAYS) -> list[dict[str, Any]]:
    return published_traffic(PAGE_TYPES, days=days)


def vitals_summary(*, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Core Web Vitals pass rates over the window.

    Buckets are summed before dividing, which is why they are stored as counts:
    averaging each day's pass rate would weigh a day with two visits the same
    as a day with two hundred.
    """

    start, end = _window(days)
    totals = VitalsDaily.objects.filter(date__gte=start, date__lte=end).aggregate(
        **{
            f"{metric}_{bucket}": Sum(f"{metric}_{bucket}")
            for metric in ("lcp", "inp", "cls")
            for bucket in ("good", "needs_improvement", "poor")
        }
    )
    summary = {}
    for metric in ("lcp", "inp", "cls"):
        good = totals.get(f"{metric}_good") or 0
        total = good + (totals.get(f"{metric}_needs_improvement") or 0)
        total += totals.get(f"{metric}_poor") or 0
        summary[metric] = {
            "good": good,
            "total": total,
            # None, not zero. No samples is not a 0% pass rate, and a page that
            # renders it as one reports a failure that was never measured.
            "rate": round(good / total, 3) if total else None,
            "percent": round(good * 100 / total, 1) if total else None,
            # The 75% line is what "passes Core Web Vitals" means. Decided here
            # so every surface draws the same threshold.
            "passing": (good / total >= 0.75) if total else None,
        }
    return summary


def latest_reading() -> date | None:
    """The most recent day any site reported, or None before the first sweep."""

    newest = RumDaily.objects.order_by("-date").values_list("date", flat=True).first()
    return newest


def last_observed_at():
    """When the controller last wrote a reading, not what day it was about.

    Two different questions, and the page needs this one. "Last reading 28 Aug"
    reads as stale on the 28th; "read 40 seconds ago" is what says the number in
    front of you is current. The controller re-reads on its own cadence, so the
    honest thing is to show when it last did rather than offer a button that
    duplicates its timer.
    """

    return (
        RumDaily.objects.order_by("-observed_at")
        .values_list("observed_at", flat=True)
        .first()
    )


def site_totals(*, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Headline numbers for the window, and how far they can be trusted."""

    start, end = _window(days)
    totals = RumDaily.objects.filter(
        dimension=RumDaily.Dimension.PATH, date__gte=start, date__lte=end
    ).aggregate(
        pageviews=Sum("pageviews"),
        visits=Sum("visits"),
        sample_interval=Max("sample_interval"),
    )
    return {
        "pageviews": totals.get("pageviews") or 0,
        "visits": totals.get("visits") or 0,
        "start": start,
        "end": end,
        "days": days,
        # Carried, not hidden. A figure extrapolated from one beacon in ten is
        # still the best number available, and is a different claim than a count.
        # Different sites can be sampled differently. Carry the least precise
        # interval so the aggregate never claims stronger evidence than any
        # number included in it.
        "sample_interval": totals.get("sample_interval") or 1,
    }


# Windows an operator can ask for, with what to call each. A fixed set rather
# than a free number because the question is always "now, or this quarter" --
# an arbitrary day count is a filter nobody tunes and every surface has to
# validate. A day is spelled "24 hours" because that is what it answers.
#
# 90 is the widest offered: the account refuses a single query wider than
# 13w2d, so a 184-day window is a request Cloudflare rejects rather than a
# reading anyone can get.
WINDOWS = ((1, "24 hours"), (7, "7 days"), (28, "28 days"), (90, "90 days"))

WINDOW_DAYS = tuple(days for days, _ in WINDOWS)

# The widest span one query may cover, as the account reports it.
MAX_QUERY_DAYS = 93

# The breakdowns the overview shows, in the order it shows them. Ordered by how
# often the answer changes what you do: what people read first, how they got
# there, then who they are.
OVERVIEW_DIMENSIONS = (
    RumDaily.Dimension.PATH,
    RumDaily.Dimension.REFERRER,
    RumDaily.Dimension.COUNTRY,
    RumDaily.Dimension.DEVICE,
    RumDaily.Dimension.BROWSER,
    RumDaily.Dimension.OS,
)


def _with_share(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each row's size relative to the largest, for drawing a bar.

    Relative to the biggest row rather than to the total, because the bar is
    there to compare rows with each other. Against a total, a breakdown with a
    long tail renders as a row of slivers that all look the same.
    """

    largest = max((row["pageviews"] for row in rows), default=0)
    return [
        row | {"share": round(row["pageviews"] * 100 / largest, 1) if largest else 0}
        for row in rows
    ]


def overview(*, days: int = DEFAULT_WINDOW_DAYS, limit: int = 12) -> dict[str, Any]:
    """Everything the analytics page shows, assembled once.

    The page is a delivery adapter over this: it chooses no numbers and joins
    nothing, so the CLI and the API can ask the same question and get the same
    answer.
    """

    days = days if days in WINDOW_DAYS else DEFAULT_WINDOW_DAYS
    totals = site_totals(days=days)
    measured_paths = measured_path_count(days=days)
    return {
        "kpis": (
            {
                "label": "Page views",
                "value": f"{totals['pageviews']:,}",
                "detail": f"{days}d",
                "is_zero": not totals["pageviews"],
            },
            {
                "label": "Visits",
                "value": f"{totals['visits']:,}",
                "detail": "",
                "is_zero": not totals["visits"],
            },
            {
                "label": "Pages reached",
                "value": f"{measured_paths:,}",
                "detail": "",
                "is_zero": not measured_paths,
            },
        ),
        "totals": totals,
        "days": days,
        "windows": [
            {"days": option, "label": label, "current": option == days}
            for option, label in WINDOWS
        ],
        "sites": list(AnalyticsSite.objects.all()),
        "latest": latest_reading(),
        "observed_at": last_observed_at(),
        "sweep_seconds": int(sweep_interval().total_seconds()),
        "vitals": vitals_summary(days=days),
        "breakdowns": [
            {
                "dimension": dimension,
                "label": RumDaily.Dimension(dimension).label,
                "rows": _with_share(breakdown(dimension, days=days, limit=limit)),
            }
            for dimension in OVERVIEW_DIMENSIONS
        ],
    }


def list_analytics(
    *, dimension: str = "", days: int = DEFAULT_WINDOW_DAYS, limit: int = 50
) -> dict[str, Any]:
    """One breakdown as a machine-readable collection.

    The same numbers the page shows, in the shape every registered resource
    returns, so the API, the MCP and the search box read this rather than
    re-deriving it. Defaults to pages, which is the breakdown anything asking
    "how is the site doing" wants first.
    """

    dimension = dimension or RumDaily.Dimension.PATH
    if dimension not in RumDaily.Dimension.values:
        raise ValueError(
            f"Unknown dimension {dimension!r}; expected one of "
            f"{', '.join(sorted(RumDaily.Dimension.values))}."
        )
    rows = breakdown(dimension, days=days, limit=limit)
    window = site_totals(days=days)
    items = [
        {
            "dimension": dimension,
            "value": row["value"],
            "pageviews": row["pageviews"],
            "visits": row["visits"],
        }
        for row in rows
    ]
    return {
        "items": items,
        "count": len(items),
        "days": days,
        "start": window["start"].isoformat(),
        "end": window["end"].isoformat(),
        # Carried into the machine surface for the same reason it is on the
        # page: a consumer that treats an extrapolation as a count will
        # eventually publish it as one.
        "sample_interval": window["sample_interval"],
    }


__all__ = [
    "CONTENT_TRAFFIC_DAYS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_QUERY_DAYS",
    "WINDOW_DAYS",
    "attach_traffic",
    "list_analytics",
    "OVERVIEW_DIMENSIONS",
    "WINDOWS",
    "overview",
    "breakdown",
    "last_observed_at",
    "latest_reading",
    "location_of",
    "measured_path_count",
    "page_traffic",
    "path_of",
    "published_traffic",
    "record_analytics",
    "site_totals",
    "vitals_summary",
    "writeup_traffic",
]
