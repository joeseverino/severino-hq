"""Readings from an AdGuard Home connection.

AdGuard's API has one admin account and no scopes, so no reading states
``requires``, and a part is refused only by the server's own settings or
answers. The query log leaves the controller only as the aggregate
``AdGuardQuerySummaryRecord`` names: per rewritten name, never a query.
"""

from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from hq.platform.application.ui import counted

from ..names import normalized_hostname
from .contract import ObservationRecord, ObservationSpec, ReadingPart

CLIENT_KIND = "adguard.client"
QUERY_KIND = "adguard.query_summary"
DNS_KIND = "adguard.dns"

# Fact keys an AdGuard reading puts on the connection that read it.
PROTECTION_OFF = "dns-protection-off"
FILTERING_OFF = "dns-filtering-off"
PLAIN_UPSTREAM = "dns-plain-upstream"
NAME_UNUSED = "dns-name-unused"

# How an upstream is reached, by the scheme AdGuard writes before it.
_TRANSPORTS = {
    "": "dns",
    "udp": "dns",
    "tcp": "dns",
    "tls": "tls",
    "https": "https",
    "h3": "https",
    "quic": "quic",
    "sdns": "dnscrypt",
}


class AdGuardClientRecord(ObservationRecord):
    connection_ref: str = ""
    name: str = ""
    # "persistent" for a client configured in AdGuard; otherwise how AdGuard
    # learned the runtime client: arp, dhcp, etc_hosts, rdns or whois.
    source: str
    # The client's identifiers: addresses, CIDRs, MACs or ClientIDs.
    ids: tuple[str, ...] = ()
    # The identifiers that are single addresses: the join keys.
    addresses: tuple[str, ...] = ()
    # Persistent clients only.
    use_global_settings: bool | None = None
    filtering_enabled: bool | None = None


class QueryClientRecord(ObservationRecord):
    address: str
    name: str = ""
    queries: int = 0


class AdGuardQuerySummaryRecord(ObservationRecord):
    """One rewritten name over the window: counts, never the queries."""

    connection_ref: str = ""
    domain: str
    queries: int = 0
    blocked: int = 0
    client_count: int = 0
    # The busiest clients, at most ``CLIENTS_KEPT``.
    clients: tuple[QueryClientRecord, ...] = ()
    last_seen: str = ""
    # The span the aggregate covers, in hours.
    window_hours: float = 0.0


class UpstreamRecord(ObservationRecord):
    # The server's host; blank for a DNS stamp, which names none readably.
    host: str = ""
    transport: str = "dns"
    # Domains this upstream answers for alone; empty for a general upstream.
    domains: tuple[str, ...] = ()


class AdGuardDnsRecord(ObservationRecord):
    connection_ref: str = ""
    version: str = ""
    running: bool | None = None
    protection_enabled: bool | None = None
    dns_addresses: tuple[str, ...] = ()
    upstreams: tuple[UpstreamRecord, ...] = ()
    upstream_mode: str = ""
    dnssec_enabled: bool | None = None
    filtering_enabled: bool | None = None
    filter_lists: int | None = None
    filter_rules: int | None = None
    rewrites_enabled: bool | None = None
    querylog_enabled: bool | None = None
    querylog_retention_hours: float | None = None
    anonymize_client_ip: bool | None = None


CLIENTS_KEPT = 10

# The busiest clients per name, which AdGuard withholds when it anonymizes.
CLIENTS_PART = ReadingPart("clients", "Clients looking it up")
# The server's posture beyond its status, each read from its own endpoint.
DNS_PARTS = (
    ReadingPart("upstreams", "Upstream resolvers"),
    ReadingPart("filtering", "Filtering"),
    ReadingPart("querylog", "Query log settings"),
    ReadingPart("rewrites", "Rewrite settings"),
)
# Clients a title names before counting the rest.
_CLIENTS_NAMED = 3


def upstream(line: Any) -> dict[str, Any] | None:
    """One upstream line as AdGuard writes it, as host, transport and domains.

    ``[/lan/]192.0.2.1`` scopes an upstream to domains; ``#`` is a comment or,
    after a scope, "the default upstreams". A URL's path and credentials are
    dropped: a DoH path can carry an account's id.
    """

    text = str(line or "").strip()
    domains: tuple[str, ...] = ()
    if text.startswith("[/") and "/]" in text:
        scope, _, text = text[2:].partition("/]")
        domains = tuple(
            name for name in (normalized_hostname(part) for part in scope.split("/")) if name
        )
        text = text.strip()
    if not text or text.startswith("#"):
        return None
    scheme, separator, rest = text.partition("://")
    if not separator:
        scheme, rest = "", text
    scheme = scheme.lower()
    if scheme not in _TRANSPORTS:
        return None
    host = "" if scheme == "sdns" else (urlsplit(f"dns://{rest}").hostname or "")
    return {"host": host, "transport": _TRANSPORTS[scheme], "domains": domains}


def is_local(host: str) -> bool:
    """Whether an upstream host is an address on this site: loopback, tailnet or private."""

    from hq.platform.application.reach import network_of

    return network_of(host) in {"loopback", "tailnet", "network"}


def plain_upstreams(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Upstreams queried over unencrypted DNS off the site."""

    return tuple(
        dict.fromkeys(
            str(item.get("host", ""))
            for item in record.get("upstreams") or ()
            if item.get("transport") == "dns" and item.get("host") and not is_local(item["host"])
        )
    )


def _dns_facts(record: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    if record.get("protection_enabled") is False:
        found.append((PROTECTION_OFF, "Off"))
    if record.get("filtering_enabled") is False:
        found.append((FILTERING_OFF, "Off"))
    found.extend((PLAIN_UPSTREAM, host) for host in plain_upstreams(record))
    return tuple(found)


# A window shorter than this says too little to call a name unused.
UNUSED_AFTER_HOURS = 12


def _query_facts(record: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    if record.get("queries"):
        return ()
    if float(record.get("window_hours") or 0) < UNUSED_AFTER_HOURS:
        return ()
    return ((NAME_UNUSED, str(record.get("domain", ""))),)


def window_phrase(hours: Any) -> str:
    """How long a window was, as a person says it."""

    try:
        value = float(hours or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 48:
        return counted(round(value / 24), "day")
    return counted(max(1, round(value)), "hour")


def _query_title(record: Mapping[str, Any]) -> str:
    window = window_phrase(record.get("window_hours"))
    queries = int(record.get("queries") or 0)
    if not queries:
        return f"Not looked up in the last {window}"
    clients = int(record.get("client_count") or 0)
    text = f"{counted(queries, 'lookup')} from {counted(clients, 'device')} in {window}"
    blocked = int(record.get("blocked") or 0)
    if blocked:
        text = f"{text}, {blocked:,} blocked"
    names = [
        str(client.get("name") or client.get("address") or "")
        for client in (record.get("clients") or ())[:_CLIENTS_NAMED]
    ]
    names = [name for name in names if name]
    if not names:
        return text
    more = clients - len(names)
    return f"{text}: {', '.join(names)}" + (f" and {more:,} more" if more > 0 else "")


def _dns_title(record: Mapping[str, Any]) -> str:
    parts = [f"AdGuard {record.get('version', '')}".strip()]
    if record.get("protection_enabled") is False:
        parts.append("protection off")
    if record.get("filtering_enabled") is False:
        parts.append("filtering off")
    return " · ".join(parts)


def _listen_addresses(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Addresses the DNS server answers on; the unspecified address names none."""

    found = []
    for item in record.get("dns_addresses") or ():
        try:
            address = ip_address(str(item))
        except ValueError:
            continue
        if not address.is_unspecified:
            found.append(str(address))
    return tuple(found)


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        CLIENT_KIND,
        "adguard",
        "DNS client",
        AdGuardClientRecord,
        addresses=lambda record: tuple(record.get("addresses") or ()),
        title=lambda record: str(
            record.get("name") or next(iter(record.get("addresses") or ()), "")
        ),
        relation="Name in AdGuard",
    ),
    ObservationSpec(
        QUERY_KIND,
        "adguard",
        "DNS lookups",
        AdGuardQuerySummaryRecord,
        # By name only: joined to a device, the aggregate would become a list of
        # what that device looks up.
        hostnames=lambda record: (str(record.get("domain", "")),),
        title=_query_title,
        relation="DNS lookups",
        facts=_query_facts,
        parts=(CLIENTS_PART,),
    ),
    ObservationSpec(
        DNS_KIND,
        "adguard",
        "DNS server",
        AdGuardDnsRecord,
        addresses=_listen_addresses,
        title=_dns_title,
        relation="DNS server",
        facts=_dns_facts,
        parts=DNS_PARTS,
    ),
)
