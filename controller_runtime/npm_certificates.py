"""The proxy's certificate store.

Which certificates NPM holds for a consumer, and how HQ names and uploads its
own there.
"""

from __future__ import annotations

from typing import Any

from control_plane.names import certificate_covers
from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from control_plane.provider_adapters import npm
from . import provider_http, provider_runtime


def npm_url(connection_ref: str = "") -> str:
    return npm.url(provider_runtime.RUNTIME, connection_ref)


def npm_token(base_url: str, connection_ref: str = "") -> str:
    return npm.token(provider_runtime.RUNTIME, base_url, connection_ref)


NPM_CERTIFICATE_IDS = "npm_certificate_ids"


def npm_certificate_name(consumer: dict[str, Any]) -> str:
    """The display name HQ gives the NPM certificate it installs for a consumer."""

    return f"Severino HQ - {consumer['name']}"


def npm_certificate_ids(
    spec: dict[str, Any], observed: dict[str, Any] | None
) -> dict[str, int]:
    """The NPM certificate id HQ installed for each NPM consumer, as last reported.

    The id is the certificate's identity in NPM; its display name is editable
    there and is not. A report from a single NPM consumer that carries only
    ``npm_certificate_id`` names that consumer's certificate.
    """

    observed = observed or {}
    consumers = [
        consumer["name"] for consumer in spec.get("consumers", ()) if consumer["kind"] == "npm"
    ]
    reported = observed.get(NPM_CERTIFICATE_IDS)
    ids = {
        str(name): value
        for name, value in (reported.items() if isinstance(reported, dict) else ())
        if type(value) is int
    }
    single = observed.get("npm_certificate_id")
    if not ids and len(consumers) == 1 and type(single) is int:
        ids = {consumers[0]: single}
    return {name: ids[name] for name in consumers if name in ids}


def with_npm_certificate_ids(
    result: ProviderResult, known: dict[str, int]
) -> ProviderResult:
    """Carry the installed certificate ids into a report that did not install one."""

    if not known or NPM_CERTIFICATE_IDS in result.status:
        return result
    return ProviderResult(
        changed=result.changed,
        status={
            **result.status,
            NPM_CERTIFICATE_IDS: known,
            "npm_certificate_id": list(known.values())[-1],
        },
        conditions=result.conditions,
        message=result.message,
    )


def npm_managed_certificate(
    consumer: dict[str, Any],
    certificate_domains: list[str],
    fullchain: bytes,
    private_key: bytes,
    known_id: int | None = None,
) -> tuple[int, dict[str, str]]:
    """Upload into the certificate HQ installed for this consumer, or a new one.

    Found by the id HQ recorded when it has one, so a certificate renamed in
    NPM is still the one updated. The display name finds it only when no id
    is recorded.
    """

    base_url = npm_url()
    headers = {"Authorization": f"Bearer {npm_token(base_url)}"}
    nice_name = npm_certificate_name(consumer)
    certificates = provider_http.request_json(f"{base_url}/nginx/certificates", headers=headers)
    matches = [
        item
        for item in certificates
        if known_id is not None and item.get("id") == known_id
    ] or [item for item in certificates if item.get("nice_name") == nice_name]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate HQ-managed certificates.")
    if matches:
        certificate = matches[0]
        if certificate.get("provider") != "other":
            raise ProviderError(
                "The HQ-managed NPM certificate is not a custom certificate."
            )
    else:
        certificate = provider_http.request_json(
            f"{base_url}/nginx/certificates",
            method="POST",
            headers=headers,
            payload={"provider": "other", "nice_name": nice_name},
        )
    certificate_id = certificate.get("id") if isinstance(certificate, dict) else None
    if not isinstance(certificate_id, int):
        raise ProviderError("NPM did not return a managed certificate ID.")

    marker = b"-----END CERTIFICATE-----"
    leaf_body, separator, chain_body = fullchain.partition(marker)
    if not separator:
        raise ProviderError("Certificate chain does not contain a leaf certificate.")
    files = {
        "certificate": ("certificate.pem", leaf_body + marker + b"\n"),
        "certificate_key": ("certificate_key.pem", private_key),
        "intermediate_certificate": (
            "intermediate_certificate.pem",
            chain_body.lstrip(),
        ),
    }
    provider_http.multipart_request(
        f"{base_url}/nginx/certificates/validate",
        headers=headers,
        files=files,
    )
    provider_http.multipart_request(
        f"{base_url}/nginx/certificates/{certificate_id}/upload",
        headers=headers,
        files=files,
    )
    hosts = provider_http.request_json(f"{base_url}/nginx/proxy-hosts", headers=headers)
    verify_domains = set(consumer["verify_domains"])
    certificate_names = set(certificate_domains)
    matching_hosts = []
    for host in hosts:
        host_domains = host.get("domain_names", [])
        explicitly_selected = bool(verify_domains.intersection(host_domains))
        discovered = consumer.get("discover_covered_hosts") and any(
            certificate_covers(domain, certificate_names) for domain in host_domains
        )
        if host.get("enabled") is not False and (explicitly_selected or discovered):
            matching_hosts.append(host)
    covered = {
        domain
        for host in matching_hosts
        for domain in host.get("domain_names", [])
        if domain in verify_domains
    }
    missing = sorted(verify_domains - covered)
    if missing:
        raise ProviderError(
            "NPM has no proxy host for managed verification names: "
            + ", ".join(missing)
            + "."
        )
    for host in matching_hosts:
        # Uploading replaces NPM's certificate files but does not reload the
        # nginx workers. Re-applying every referencing host activates them.
        provider_http.request_json(
            f"{base_url}/nginx/proxy-hosts/{host['id']}",
            method="PUT",
            headers=headers,
            payload={"certificate_id": certificate_id},
        )
    return certificate_id, {"nice_name": nice_name}
