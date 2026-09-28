"""Site analytics read through Cloudflare's GraphQL API."""

from __future__ import annotations

from datetime import date
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from analytics.contracts import MAX_QUERY_DAYS, completed_window
from control_plane.provider_adapters.contracts import ProviderError
from . import cloudflare, connection_env, provider_http


# Which Cloudflare dimension answers each breakdown HQ stores. Declared once:
# the GraphQL query is generated from this and so is the payload, so a new
# breakdown is one entry here and one enum member in the analytics app, and
# there is no third place where the two names could stop matching.
ANALYTICS_DIMENSIONS = {
    "path": "requestPath",
    "referrer": "refererHost",
    "country": "countryName",
    "device": "deviceType",
    "browser": "userAgentBrowser",
    "os": "userAgentOS",
}

# Percentiles HQ keeps, and the field each comes from. p75 because that is the
# threshold Core Web Vitals is actually defined at: a metric passes when 75%
# of samples are good, so the 75th percentile is the number being judged.
ANALYTICS_VITALS = {
    "largest_contentful_paint_ms": "largestContentfulPaintP75",
    "interaction_to_next_paint_ms": "interactionToNextPaintP75",
    "first_contentful_paint_ms": "firstContentfulPaintP75",
    "time_to_first_byte_ms": "timeToFirstByteP75",
}

ANALYTICS_BUCKETS = ("lcp", "inp", "cls")


def _cloudflare_graphql(
    query: str, variables: dict[str, Any], connection_ref: str = ""
) -> dict[str, Any]:
    """One GraphQL call against the account credential.

    GraphQL answers 200 with an ``errors`` array rather than an HTTP status, so
    a caller that only checked the status would read a failed query as an empty
    estate, which is indistinguishable from a site nobody visited.
    """

    prefix = connection_env.connection_prefix("cloudflare_api", connection_ref)
    cloudflare.cloudflare_breaker(prefix)
    base = cloudflare.cloudflare_url(connection_ref, provider="cloudflare_api")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    try:
        with provider_http.open_url(
            f"{base}/graphql",
            data=body,
            method="POST",
            headers={
                "Authorization": (
                    f"Bearer {cloudflare.cloudflare_token(connection_ref, provider='cloudflare_api')}"
                ),
                "Content-Type": "application/json",
            },
            timeout=30,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            detail = cloudflare.cloudflare_errors(exc.read())
        raise cloudflare.cloudflare_refused(
            prefix,
            f"Cloudflare analytics refused the query: HTTP {exc.code}.",
            detail,
            status=exc.code,
            verified=lambda: cloudflare.cloudflare_verified("cloudflare_api", connection_ref),
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(
            f"Cloudflare analytics was unreachable: {type(exc).__name__}."
        ) from exc
    except ValueError as exc:
        raise ProviderError("Cloudflare analytics returned invalid JSON.") from exc

    if payload.get("errors"):
        first = payload["errors"][0]
        message = first.get("message", "") if isinstance(first, dict) else ""
        raise cloudflare.cloudflare_refused(
            prefix, f"Cloudflare analytics rejected the query: {message}", message
        )
    return payload.get("data") or {}


def analytics_account(connection_ref: str = "") -> str:
    """The one account this credential reads, discovered rather than configured."""

    accounts = cloudflare.cloudflare_api_list("/accounts", connection_ref, per_page=50)
    tags = [account["id"] for account in accounts if account.get("id")]
    if len(tags) != 1:
        raise ProviderError(
            f"The Cloudflare credential sees {len(tags)} accounts; it has to see one."
        )
    return tags[0]


def _analytics_sites(account: str, connection_ref: str = "") -> list[dict[str, str]]:
    """Web Analytics sites that still describe something.

    The same membership rule the probe applies: a site whose ruleset names no
    hostname measures nothing, and Cloudflare keeps those around indefinitely.
    """

    result = cloudflare.cloudflare_api_list(
        f"/accounts/{account}/rum/site_info/list", connection_ref
    )
    sites = []
    for site in result:
        if not site.get("site_tag"):
            continue
        ruleset = site.get("ruleset") or {}
        host = str(ruleset.get("zone_name") or "").strip().rstrip(".").lower()
        if host:
            sites.append({"site_tag": str(site["site_tag"]), "host": host})
    return sorted(sites, key=lambda item: item["host"])


def _analytics_query() -> str:
    """One query carrying every breakdown, built from the dimension registry.

    Aliased selections rather than a request each: the breakdowns share a
    filter and a window, and asking six times would spend six times the quota
    to answer one question about one day.
    """

    breakdowns = "\n".join(
        f"""{name}: rumPageloadEventsAdaptiveGroups(
             filter: $filter, limit: 5000, orderBy: [count_DESC]
           ) {{
             count
             sum {{ visits }}
             avg {{ sampleInterval }}
             dimensions {{ date {field} }}
           }}"""
        for name, field in ANALYTICS_DIMENSIONS.items()
    )
    quantiles = " ".join(ANALYTICS_VITALS.values())
    buckets = " ".join(
        f"{metric}{suffix}"
        for metric in ANALYTICS_BUCKETS
        for suffix in ("Good", "NeedsImprovement", "Poor")
    )
    return f"""
      query($account: String!, $filter: ZoneRumPageloadEventsAdaptiveGroupsFilter_InputObject!,
            $vitalsFilter: ZoneRumWebVitalsEventsAdaptiveGroupsFilter_InputObject!) {{
        viewer {{
          accounts(filter: {{ accountTag: $account }}) {{
            {breakdowns}
            vitals: rumWebVitalsEventsAdaptiveGroups(
              filter: $vitalsFilter, limit: 5000, orderBy: [date_ASC]
            ) {{
              count
              avg {{ sampleInterval }}
              quantiles {{ {quantiles} cumulativeLayoutShiftP75 }}
              sum {{ {buckets} }}
              dimensions {{ date }}
            }}
          }}
        }}
      }}
    """


def _milliseconds(value: Any) -> int | None:
    """Cloudflare's microseconds as milliseconds, and its -1 as absence.

    A percentile with no samples arrives as -1, which is absence wearing a
    number's clothes. Left alone it would average into a negative load time,
    so it stops here rather than downstream.
    """

    if value is None:
        return None
    try:
        micros = float(value)
    except (TypeError, ValueError):
        return None
    if micros < 0:
        return None
    return int(round(micros / 1000))


def _analytics_rows(account: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise every configured traffic breakdown from one query result."""

    rows = []
    for dimension, field in ANALYTICS_DIMENSIONS.items():
        for group in account.get(dimension) or []:
            dimensions = group.get("dimensions") or {}
            value = str(dimensions.get(field) or "").strip()
            if not value:
                continue
            rows.append(
                {
                    "dimension": dimension,
                    "value": value[:512],
                    "date": dimensions.get("date"),
                    "pageviews": int(group.get("count") or 0),
                    "visits": int((group.get("sum") or {}).get("visits") or 0),
                    "sample_interval": int(
                        (group.get("avg") or {}).get("sampleInterval") or 1
                    ),
                }
            )
    return rows


def _analytics_vitals(account: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise site-wide vitals from one query result."""

    vitals = []
    for group in account.get("vitals") or []:
        quantiles = group.get("quantiles") or {}
        sums = group.get("sum") or {}
        reading = {
            "date": (group.get("dimensions") or {}).get("date"),
            "sample_interval": int((group.get("avg") or {}).get("sampleInterval") or 1),
            "cumulative_layout_shift": quantiles.get("cumulativeLayoutShiftP75"),
        }
        for column, field in ANALYTICS_VITALS.items():
            reading[column] = _milliseconds(quantiles.get(field))
        for metric in ANALYTICS_BUCKETS:
            for suffix, column in (
                ("Good", "good"),
                ("NeedsImprovement", "needs_improvement"),
                ("Poor", "poor"),
            ):
                reading[f"{metric}_{column}"] = int(sums.get(f"{metric}{suffix}") or 0)
        vitals.append(reading)
    return vitals


def _analytics_site_reading(
    account: str,
    site: dict[str, str],
    connection_ref: str,
    *,
    start: date,
    end: date,
    query: str,
) -> dict[str, Any]:
    """One site's reading, with its connection identity preserved."""

    window = {
        "siteTag": site["site_tag"],
        "date_geq": start.isoformat(),
        "date_leq": end.isoformat(),
    }
    data = _cloudflare_graphql(
        query,
        {"account": account, "filter": window, "vitalsFilter": dict(window)},
        connection_ref,
    )
    accounts = (data.get("viewer") or {}).get("accounts") or []
    if len(accounts) != 1 or not isinstance(accounts[0], dict):
        raise ProviderError("Cloudflare analytics returned no matching account.")
    result = accounts[0]
    return {
        "site_tag": site["site_tag"],
        "host": site["host"],
        "connection_ref": connection_ref,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": _analytics_rows(result),
        "vitals": _analytics_vitals(result),
    }


def analytics_sites() -> list[dict[str, str]]:
    """Discover measured sites once so HQ can plan their missing windows."""

    found = []
    for connection_ref in connection_env.provider_connection_refs("cloudflare_api"):
        account = analytics_account(connection_ref)
        found.extend(
            {
                **site,
                "account": account,
                "connection_ref": connection_ref,
            }
            for site in _analytics_sites(account, connection_ref)
        )
    return found


def analytics(
    days: int = 3,
    *,
    sites: list[dict[str, str]] | None = None,
    windows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Every site's recent traffic and vitals, in the shape HQ stores.

    ``days`` is short by default because a day that has closed does not change:
    re-reading a week on every sweep would spend quota confirming numbers that
    were settled the first time. A longer window is what a backfill asks for,
    and the same call answers it.

    Returns whole days only. The current day is excluded because it is still
    accumulating, and a partial day stored beside complete ones is the kind of
    figure that reads as a traffic collapse every morning.
    """

    sources = analytics_sites() if sites is None else sites
    if not sources:
        # Nothing to do, which is not the same as something going wrong. A
        # deployment carrying no analytics credential should sweep in silence
        # rather than report a failure on every pass.
        return {"sites": []}

    default_start, completed = completed_window(days)
    query = _analytics_query()
    planned = {
        (item.get("connection_ref", ""), item.get("site_tag", "")): item
        for item in (windows or [])
        if isinstance(item, dict)
    }
    readings = []
    for site in sources:
        window = planned.get((site["connection_ref"], site["site_tag"]), {})
        try:
            start = date.fromisoformat(window.get("start", ""))
            end = date.fromisoformat(window.get("end", ""))
        except (TypeError, ValueError):
            start, end = default_start, completed
        if start > end or end > completed or (end - start).days >= MAX_QUERY_DAYS:
            start, end = default_start, completed
        readings.append(
            _analytics_site_reading(
                site["account"],
                site,
                site["connection_ref"],
                start=start,
                end=end,
                query=query,
            )
        )
    return {"sites": readings}
