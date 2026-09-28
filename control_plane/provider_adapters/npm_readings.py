"""Nginx Proxy Manager readings: certificates, redirects, streams, access lists
and 404 hosts, from each NPM connection.

Requests go through the adapter's session on the controller's runtime, and each
list is read once per sweep and shared: the certificate reading needs every
host list to say which names a certificate serves. A host list the credential
may not see is a refused part of the reading that needs it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..names import normalized_hostname
from ..observations.npm import (
    ACCESS_LIST_KIND,
    CERTIFICATE_KIND,
    DEAD_HOST_KIND,
    PROTECTED_HOSTS_PART,
    REDIRECT_KIND,
    SERVING_PARTS,
    STREAM_KIND,
)
from .contracts import ProviderError, ProviderRuntime
from .parts import refuse_part
from .refusals import refused

# Each list, the NPM permission area that shows it, and what the list is called.
PROXY_HOSTS = ("/nginx/proxy-hosts", "proxy_hosts: view", "The proxy host list")
REDIRECTION_HOSTS = (
    "/nginx/redirection-hosts",
    "redirection_hosts: view",
    "The redirection host list",
)
DEAD_HOSTS = ("/nginx/dead-hosts", "dead_hosts: view", "The 404 host list")
STREAMS = ("/nginx/streams", "streams: view", "The stream list")
CERTIFICATES = ("/nginx/certificates", "certificates: view", "The certificate list")
ACCESS_LISTS = (
    "/nginx/access-lists?expand=items,clients",
    "access_lists: view",
    "The access list list",
)

# Lists whose hosts can serve a certificate, each as the certificate reading's
# declared part.
_SERVING = tuple(
    (source, part.name)
    for source in (PROXY_HOSTS, REDIRECTION_HOSTS, DEAD_HOSTS, STREAMS)
    for part in SERVING_PARTS
    if part.requires == (source[1],)
)


def refs(runtime: ProviderRuntime) -> tuple[str, ...]:
    """Every NPM connection, or the sole unlabelled one."""

    return runtime.connection_refs("npm") or ("",)


def listed(runtime: ProviderRuntime, ref: str, source: tuple[str, str, str]) -> list[dict[str, Any]]:
    path, needs, what = source
    # Imported here: the adapter admits these readings at its own import, so a
    # module-level import back into it would leave READINGS undefined for
    # whichever of the two loads second.
    from . import npm as adapter

    def load() -> list[dict[str, Any]]:
        try:
            base_url, headers = adapter.session(runtime, ref)
            found = runtime.request(f"{base_url}{path}", headers=headers)
        except (ProviderError, OSError, ValueError) as exc:
            raise refused(exc, what=what, needs=needs) from exc
        return [item for item in found or () if isinstance(item, dict)]

    return runtime.snapshot_value(("npm-list", ref, path), load)


def _names(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(name for name in (normalized_hostname(v) for v in values or ()) if name)
    )


def _by_ref(
    runtime: ProviderRuntime,
    each: Callable[[ProviderRuntime, str], Iterable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [
        {"connection_ref": ref, **record}
        for ref in refs(runtime)
        for record in each(runtime, ref)
    ]


def _certificate_names(runtime: ProviderRuntime, ref: str) -> dict[int, str]:
    """Certificate names by id, where the credential may list them."""

    try:
        return {
            item["id"]: str(item.get("nice_name", "") or "")
            for item in listed(runtime, ref, CERTIFICATES)
            if isinstance(item.get("id"), int)
        }
    except ProviderError:
        return {}


# ----- Certificates ------------------------------------------------------------


def _serves(runtime: ProviderRuntime, ref: str) -> dict[int, list[str]]:
    """The names each certificate id is served for. A host list the credential
    may not see is refused as its part."""

    serves: dict[int, list[str]] = {}
    for source, part in _SERVING:
        try:
            hosts = listed(runtime, ref, source)
        except ProviderError as exc:
            refuse_part(part, exc, connection_ref=ref)
            continue
        for host in hosts:
            certificate = host.get("certificate_id")
            if isinstance(certificate, int) and certificate and host.get("enabled", True):
                serves.setdefault(certificate, []).extend(host.get("domain_names") or ())
    return serves


def certificates(runtime: ProviderRuntime, ref: str) -> list[dict[str, Any]]:
    listed_certificates = listed(runtime, ref, CERTIFICATES)
    serves = _serves(runtime, ref)
    return [
        {
            "id": item["id"],
            "name": str(item.get("nice_name", "") or ""),
            "provider": str(item.get("provider", "") or ""),
            "domains": tuple(str(name) for name in item.get("domain_names") or ()),
            "expires_on": str(item.get("expires_on", "") or ""),
            "serves": _names(serves.get(item["id"], ())),
        }
        for item in listed_certificates
        if isinstance(item.get("id"), int)
    ]


# ----- Hosts that are not proxies -----------------------------------------------


def _target(scheme: str, host: str) -> str:
    """Where a redirection host sends a visitor; "auto" keeps the visitor's scheme."""

    if not host:
        return ""
    return f"{scheme}://{host}" if scheme in ("http", "https") else host


def redirects(runtime: ProviderRuntime, ref: str) -> list[dict[str, Any]]:
    names = _certificate_names(runtime, ref)
    return [
        {
            "id": item["id"],
            "hostnames": _names(item.get("domain_names")),
            "target": _target(
                str(item.get("forward_scheme", "") or ""),
                str(item.get("forward_domain_name", "") or ""),
            ),
            "target_host": normalized_hostname(item.get("forward_domain_name")),
            "status_code": item.get("forward_http_code"),
            "preserve_path": bool(item.get("preserve_path")),
            "ssl_forced": bool(item.get("ssl_forced")),
            "certificate": names.get(item.get("certificate_id"), ""),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in listed(runtime, ref, REDIRECTION_HOSTS)
        if isinstance(item.get("id"), int)
    ]


def dead_hosts(runtime: ProviderRuntime, ref: str) -> list[dict[str, Any]]:
    names = _certificate_names(runtime, ref)
    return [
        {
            "id": item["id"],
            "hostnames": _names(item.get("domain_names")),
            "certificate": names.get(item.get("certificate_id"), ""),
            "ssl_forced": bool(item.get("ssl_forced")),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in listed(runtime, ref, DEAD_HOSTS)
        if isinstance(item.get("id"), int)
    ]


def streams(runtime: ProviderRuntime, ref: str) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "incoming_port": item.get("incoming_port"),
            "forwarding_host": str(item.get("forwarding_host", "") or ""),
            "forwarding_port": item.get("forwarding_port"),
            "tcp": bool(item.get("tcp_forwarding")),
            "udp": bool(item.get("udp_forwarding")),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in listed(runtime, ref, STREAMS)
        if isinstance(item.get("id"), int) and isinstance(item.get("incoming_port"), int)
    ]


def access_lists(runtime: ProviderRuntime, ref: str) -> list[dict[str, Any]]:
    listed_lists = listed(runtime, ref, ACCESS_LISTS)
    protects: dict[int, list[str]] = {}
    try:
        for host in listed(runtime, ref, PROXY_HOSTS):
            if isinstance(host.get("access_list_id"), int) and host.get("access_list_id"):
                protects.setdefault(host["access_list_id"], []).extend(host.get("domain_names") or ())
    except ProviderError as exc:
        refuse_part(PROTECTED_HOSTS_PART.name, exc, connection_ref=ref)
    found = []
    for item in listed_lists:
        if not isinstance(item.get("id"), int):
            continue
        record = {
            "id": item["id"],
            "name": str(item.get("name", "") or ""),
            "satisfy_any": bool(item.get("satisfy_any")),
            "pass_auth": bool(item.get("pass_auth")),
            "clients": tuple(
                {"directive": str(rule.get("directive", "")), "address": str(rule.get("address", ""))}
                for rule in item.get("clients") or ()
                if isinstance(rule, dict) and rule.get("directive") and rule.get("address")
            ),
            "logins": tuple(
                str(login.get("username", ""))
                for login in item.get("items") or ()
                if isinstance(login, dict) and login.get("username")
            ),
            "protects": _names(protects.get(item["id"], ())),
        }
        found.append(record)
    return found


def _each(read: Callable[[ProviderRuntime, str], Iterable[dict[str, Any]]]):
    return lambda runtime: _by_ref(runtime, read)


# The readings this module answers, each over every NPM connection; the NPM
# adapter declares them.
READINGS = {
    CERTIFICATE_KIND: _each(certificates),
    REDIRECT_KIND: _each(redirects),
    STREAM_KIND: _each(streams),
    ACCESS_LIST_KIND: _each(access_lists),
    DEAD_HOST_KIND: _each(dead_hosts),
}
