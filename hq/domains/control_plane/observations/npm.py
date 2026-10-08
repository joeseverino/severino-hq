"""Readings from the Nginx Proxy Manager login, beside its proxy hosts.

A certificate joins the names NPM serves with it, not every name it covers: a
wildcard NPM holds says nothing about a name another ingress answers. Key
material (a certificate's ``meta``) and access list passwords are never
named, so the schema drops them.

``requires`` names NPM's own permission areas at the ``view`` level.
"""

from collections.abc import Mapping
from typing import Any

from hq.platform.core.network import is_address

from ..certificate_authorities import authority_name
from ..names import normalized_hostname
from .cloudflare import redirect_target
from .contract import ObservationRecord, ObservationSpec, ReadingPart

PROVIDER = "npm"

CERTIFICATE_KIND = "npm.certificate"
REDIRECT_KIND = "npm.redirect"
STREAM_KIND = "npm.stream"
ACCESS_LIST_KIND = "npm.access_list"
DEAD_HOST_KIND = "npm.dead_host"

# The host lists that say which names a certificate serves, each a part of the
# certificate reading, and the one that says which names an access list guards.
SERVING_PARTS = (
    ReadingPart("proxy_hosts", "Names served by proxy hosts", ("proxy_hosts: view",)),
    ReadingPart(
        "redirection_hosts", "Names served by redirection hosts", ("redirection_hosts: view",)
    ),
    ReadingPart("dead_hosts", "Names served by 404 hosts", ("dead_hosts: view",)),
    ReadingPart("streams", "Streams served", ("streams: view",)),
)
PROTECTED_HOSTS_PART = ReadingPart(
    "proxy_hosts", "Proxy hosts behind it", ("proxy_hosts: view",)
)


def _names(values: Any) -> tuple[str, ...]:
    found: list[str] = []
    for value in values or ():
        name = normalized_hostname(value)
        if name and name not in found:
            found.append(name)
    return tuple(found)


class CertificateRecord(ObservationRecord):
    connection_ref: str = ""
    id: int
    name: str = ""
    # "letsencrypt" or "other", as NPM reports it.
    provider: str = ""
    domains: tuple[str, ...] = ()
    expires_on: str = ""
    # The names NPM answers with this certificate: proxy, redirection and 404 hosts.
    serves: tuple[str, ...] = ()


class RedirectRecord(ObservationRecord):
    connection_ref: str = ""
    id: int
    hostnames: tuple[str, ...] = ()
    target: str = ""
    target_host: str = ""
    status_code: int | None = None
    preserve_path: bool = False
    ssl_forced: bool = False
    certificate: str = ""
    enabled: bool = True


class StreamRecord(ObservationRecord):
    connection_ref: str = ""
    id: int
    incoming_port: int
    forwarding_host: str = ""
    forwarding_port: int | None = None
    tcp: bool = False
    udp: bool = False
    enabled: bool = True


class AccessRule(ObservationRecord):
    # "allow" or "deny".
    directive: str
    address: str


class AccessListRecord(ObservationRecord):
    connection_ref: str = ""
    id: int
    name: str = ""
    # Whether either the address rules or a login admits a client.
    satisfy_any: bool = False
    pass_auth: bool = False
    clients: tuple[AccessRule, ...] = ()
    # Login names only; NPM never returns the passwords and none is kept.
    logins: tuple[str, ...] = ()
    # The proxy host names it guards.
    protects: tuple[str, ...] = ()


class DeadHostRecord(ObservationRecord):
    connection_ref: str = ""
    id: int
    hostnames: tuple[str, ...] = ()
    certificate: str = ""
    ssl_forced: bool = False
    enabled: bool = True


def _enabled_names(record: Mapping[str, Any]) -> tuple[str, ...]:
    return _names(record.get("hostnames")) if record.get("enabled", True) else ()


def _stream_target(record: Mapping[str, Any]) -> str:
    host = str(record.get("forwarding_host", "") or "")
    port = record.get("forwarding_port")
    return f"{host}:{port}" if host and port else host


def _stream_title(record: Mapping[str, Any]) -> str:
    protocols = "/".join(
        name for name, on in (("TCP", record.get("tcp")), ("UDP", record.get("udp"))) if on
    )
    return f"{protocols or 'Stream'} {record.get('incoming_port', '')} to {_stream_target(record)}"


def _stream_hosts(record: Mapping[str, Any]) -> tuple[str, ...]:
    name = normalized_hostname(record.get("forwarding_host"))
    return (name,) if name and "." in name and not is_address(name) else ()


def _stream_addresses(record: Mapping[str, Any]) -> tuple[str, ...]:
    host = str(record.get("forwarding_host", "") or "")
    return (host,) if is_address(host) else ()


def stream_upstream(record: Mapping[str, Any], hostname: str = "") -> str:
    del hostname
    return _stream_target(record) if record.get("enabled", True) else ""


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        CERTIFICATE_KIND,
        PROVIDER,
        "NPM certificate",
        CertificateRecord,
        requires=(
            "certificates: view",
            *(name for part in SERVING_PARTS for name in part.requires),
        ),
        parts=SERVING_PARTS,
        hostnames=lambda record: _names(record.get("serves")),
        title=lambda record: str(record.get("name", "")),
        relation="Served with certificate",
        facet="certificate",
        short_label="NPM",
        expires=lambda record: str(record.get("expires_on", "")),
        issuer=lambda record: authority_name(record.get("provider")),
    ),
    ObservationSpec(
        REDIRECT_KIND,
        PROVIDER,
        "NPM redirect",
        RedirectRecord,
        requires=("redirection_hosts: view",),
        hostnames=_enabled_names,
        title=lambda record: str(record.get("target_host") or record.get("target") or ""),
        relation="Redirects to",
        # Answered at the ingress on the machine, not at an edge.
        facet="proxy",
        names_services=True,
        redirects_to=redirect_target,
    ),
    ObservationSpec(
        STREAM_KIND,
        PROVIDER,
        "NPM stream",
        StreamRecord,
        requires=("streams: view",),
        hostnames=_stream_hosts,
        addresses=_stream_addresses,
        title=_stream_title,
        relation="Receives a forward through",
        upstream=stream_upstream,
    ),
    ObservationSpec(
        ACCESS_LIST_KIND,
        PROVIDER,
        "NPM access list",
        AccessListRecord,
        requires=("access_lists: view", *PROTECTED_HOSTS_PART.requires),
        parts=(PROTECTED_HOSTS_PART,),
        hostnames=lambda record: _names(record.get("protects")),
        title=lambda record: str(record.get("name", "")),
        relation="Behind access list",
        restricts=True,
    ),
    ObservationSpec(
        DEAD_HOST_KIND,
        PROVIDER,
        "NPM dead host",
        DeadHostRecord,
        requires=("dead_hosts: view",),
        hostnames=_enabled_names,
        title=lambda record: ", ".join(record.get("hostnames") or ()),
        relation="Answers 404 through",
        facet="proxy",
        names_services=True,
    ),
)
