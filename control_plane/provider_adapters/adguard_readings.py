"""The AdGuard readings: clients, the query-log aggregate, and the server's DNS posture.

Each connection is read on its own and its records carry its ``connection_ref``.
The query log is reduced here, in memory, to one row per name AdGuard rewrites;
no query, timestamp per query, answer or other name leaves this module.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import quote

from ..names import in_zone, normalized_hostname
from ..observations.adguard import CLIENTS_KEPT, CLIENTS_PART, DNS_PARTS, upstream
from .contracts import ProviderError, ProviderRuntime
from .parts import refuse_part

# The span the query aggregate covers, and the most of the log it reads.
QUERY_WINDOW = timedelta(hours=24)
QUERY_PAGE = 500
QUERY_PAGES = 10


def _refs(runtime: ProviderRuntime) -> tuple[str, ...]:
    # No declared connection still reads once, so a missing credential raises.
    return runtime.connection_refs("adguard") or ("",)


def url(runtime: ProviderRuntime, ref: str = "") -> str:
    prefix = runtime.connection_prefix("adguard", ref)
    return runtime.required(prefix, "URL").rstrip("/")


def headers(runtime: ProviderRuntime, ref: str = "") -> dict[str, str]:
    prefix = runtime.connection_prefix("adguard", ref)
    encoded = base64.b64encode(
        f"{runtime.required(prefix, 'USERNAME')}:{runtime.required(prefix, 'PASSWORD')}".encode()
    ).decode()
    return {"Authorization": f"Basic {encoded}"}


def fetch(runtime: ProviderRuntime, ref: str, path: str) -> Any:
    return runtime.request(f"{url(runtime, ref)}{path}", headers=headers(runtime, ref))


def rewrites(runtime: ProviderRuntime, ref: str) -> list[dict[str, Any]]:
    """One connection's rewrite list, read once per sweep."""

    return runtime.snapshot_value(
        ("adguard-rewrites", ref),
        lambda: list(fetch(runtime, ref, "/control/rewrite/list") or ()),
    )


# ----- Clients ---------------------------------------------------------------


def _address(value: Any) -> str:
    try:
        return str(ip_address(str(value).strip()))
    except ValueError:
        return ""


def client_records(payload: Any, ref: str) -> list[dict[str, Any]]:
    """Persistent clients, then runtime clients no persistent client already names."""

    payload = payload if isinstance(payload, dict) else {}
    found: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for client in payload.get("clients") or ():
        ids = tuple(str(item).strip() for item in client.get("ids") or () if str(item).strip())
        addresses = tuple(dict.fromkeys(a for a in map(_address, ids) if a))
        claimed.update(addresses)
        found.append(
            {
                "connection_ref": ref,
                "name": str(client.get("name") or ""),
                "source": "persistent",
                "ids": ids,
                "addresses": addresses,
                "use_global_settings": client.get("use_global_settings"),
                "filtering_enabled": client.get("filtering_enabled"),
            }
        )
    for client in payload.get("auto_clients") or ():
        address = _address(client.get("ip"))
        if not address or address in claimed:
            continue
        claimed.add(address)
        found.append(
            {
                "connection_ref": ref,
                "name": str(client.get("name") or ""),
                "source": str(client.get("source") or "").lower(),
                "ids": (address,),
                "addresses": (address,),
            }
        )
    return found


def read_clients(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for ref in _refs(runtime):
        found.extend(
            client_records(fetch(runtime, ref, "/control/clients"), ref)
        )
    return found


# ----- Query log aggregate ---------------------------------------------------


def _moment(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


class _Names:
    """The names the rewrites answer for, with AdGuard's wildcard meaning."""

    def __init__(self, domains):
        names = {normalized_hostname(domain) for domain in domains} - {""}
        self.exact = frozenset(name for name in names if not name.startswith("*."))
        self.zones = tuple(sorted(name[2:] for name in names if name.startswith("*.")))

    def covers(self, name: str) -> bool:
        if name in self.exact:
            return True
        return any(name != zone and in_zone(name, zone) for zone in self.zones)


class _Tally:
    """Counts for one name: never the queries themselves."""

    def __init__(self):
        self.queries = 0
        self.blocked = 0
        self.last_seen: datetime | None = None
        self.clients: dict[str, list] = {}

    def add(self, entry: dict[str, Any], when: datetime) -> None:
        self.queries += 1
        if str(entry.get("reason") or "").startswith("Filtered"):
            self.blocked += 1
        if self.last_seen is None or when > self.last_seen:
            self.last_seen = when
        address = _address(entry.get("client")) or str(entry.get("client_id") or "")
        if address:
            name = str((entry.get("client_info") or {}).get("name") or "")
            seen = self.clients.setdefault(address, [name, 0])
            seen[0] = seen[0] or name
            seen[1] += 1

    def record(self, domain: str, ref: str, hours: float, anonymized: bool) -> dict[str, Any]:
        busiest = sorted(self.clients.items(), key=lambda item: (-item[1][1], item[0]))
        found = {
            "connection_ref": ref,
            "domain": domain,
            "queries": self.queries,
            "blocked": self.blocked,
            "client_count": len(self.clients),
            "last_seen": self.last_seen.isoformat() if self.last_seen else "",
            "window_hours": round(hours, 1),
        }
        if not anonymized:
            found["clients"] = tuple(
                {"address": address, "name": name, "queries": count}
                for address, (name, count) in busiest[:CLIENTS_KEPT]
            )
        return found


def _query_pages(get: Callable[[str], Any], cutoff: datetime, span: dict[str, bool]):
    """The log's entries, newest first, down to ``cutoff`` or the page limit.

    Yields ``(entry, moment)``. ``span["full"]`` is set when the cutoff was
    reached, so the window is the whole of ``QUERY_WINDOW``.
    """

    older_than = ""
    for _page in range(QUERY_PAGES):
        path = f"/control/querylog?limit={QUERY_PAGE}"
        if older_than:
            path += f"&older_than={quote(older_than)}"
        answer = get(path) or {}
        entries = answer.get("data") or ()
        for entry in entries:
            when = _moment(entry.get("time"))
            if when is None:
                continue
            if when < cutoff:
                span["full"] = True
                return
            yield entry, when
        older_than = str(answer.get("oldest") or "")
        if len(entries) < QUERY_PAGE or not older_than:
            return


def summarize(
    get: Callable[[str], Any],
    domains,
    ref: str,
    *,
    now: datetime,
    anonymized: bool,
) -> list[dict[str, Any]]:
    """One record per rewritten name over the window, including names nobody queried.

    The window is ``QUERY_WINDOW`` when the log reaches back that far, and
    otherwise the span the log holds.
    """

    names = _Names(domains)
    tallies: dict[str, _Tally] = {name: _Tally() for name in sorted(names.exact)}
    oldest = now
    span = {"full": False}
    for entry, when in _query_pages(get, now - QUERY_WINDOW, span):
        oldest = min(oldest, when)
        name = normalized_hostname((entry.get("question") or {}).get("name"))
        if name and names.covers(name):
            tallies.setdefault(name, _Tally()).add(entry, when)
    covered = QUERY_WINDOW if span["full"] else now - oldest
    hours = max(covered.total_seconds() / 3600, 0.0)
    return [
        tally.record(domain, ref, hours, anonymized)
        for domain, tally in sorted(tallies.items())
    ]


def read_query_summary(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for ref in _refs(runtime):

        def get(path: str, ref: str = ref) -> Any:
            return fetch(runtime, ref, path)

        config = get("/control/querylog/config") or {}
        if config.get("enabled") is False:
            raise ProviderError(
                "AdGuard's query log is off, so HQ cannot tell which names are used."
            )
        domains = [
            item.get("domain", "")
            for item in rewrites(runtime, ref)
            if item.get("enabled", True) is not False
        ]
        anonymized = config.get("anonymize_client_ip") is True
        if anonymized:
            refuse_part(
                CLIENTS_PART.name,
                ProviderError("AdGuard anonymizes client addresses."),
                connection_ref=ref,
            )
        found.extend(summarize(get, domains, ref, now=now, anonymized=anonymized))
    return found


# ----- DNS posture ------------------------------------------------------------


def _status_part(status: Any) -> dict[str, Any]:
    status = status if isinstance(status, dict) else {}
    return {
        "version": str(status.get("version") or ""),
        "running": status.get("running"),
        "protection_enabled": status.get("protection_enabled"),
        "dns_addresses": tuple(str(item) for item in status.get("dns_addresses") or ()),
    }


def _upstream_part(info: Any) -> dict[str, Any]:
    info = info if isinstance(info, dict) else {}
    parsed = (upstream(line) for line in info.get("upstream_dns") or ())
    return {
        "upstreams": tuple(item for item in parsed if item is not None),
        "upstream_mode": str(info.get("upstream_mode") or ""),
        "dnssec_enabled": info.get("dnssec_enabled"),
    }


def _filtering_part(status: Any) -> dict[str, Any]:
    status = status if isinstance(status, dict) else {}
    lists = [item for item in status.get("filters") or () if item.get("enabled")]
    return {
        "filtering_enabled": status.get("enabled"),
        "filter_lists": len(lists),
        "filter_rules": sum(int(item.get("rules_count") or 0) for item in lists),
    }


def _querylog_part(config: Any) -> dict[str, Any]:
    config = config if isinstance(config, dict) else {}
    interval = config.get("interval")
    return {
        "querylog_enabled": config.get("enabled"),
        "querylog_retention_hours": (
            round(float(interval) / 3_600_000, 2) if isinstance(interval, (int, float)) else None
        ),
        "anonymize_client_ip": config.get("anonymize_client_ip"),
    }


def _rewrite_part(settings: Any) -> dict[str, Any]:
    return {"rewrites_enabled": (settings or {}).get("enabled")}


# Each declared part of the record, the path it is read from, and how it is
# read. The status is not optional: a server that answers nothing else raises.
_DNS_READS = {
    "upstreams": ("/control/dns_info", _upstream_part),
    "filtering": ("/control/filtering/status", _filtering_part),
    "querylog": ("/control/querylog/config", _querylog_part),
    "rewrites": ("/control/rewrite/settings", _rewrite_part),
}


def dns_record(get: Callable[[str], Any], ref: str) -> dict[str, Any]:
    """The server's posture. A part it would not answer is refused on its own."""

    record: dict[str, Any] = {"connection_ref": ref, **_status_part(get("/control/status"))}
    for part in DNS_PARTS:
        path, read = _DNS_READS[part.name]
        try:
            record.update(read(get(path)))
        except (ProviderError, OSError, ValueError, TypeError, AttributeError) as exc:
            refuse_part(part.name, exc, connection_ref=ref)
    return record


def read_dns(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    return [
        dns_record(lambda path, ref=ref: fetch(runtime, ref, path), ref)
        for ref in _refs(runtime)
    ]
