"""Fail-closed provider adapters used only by the host-side controller."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class ProviderError(RuntimeError):
    """A provider operation failed without exposing credential material."""


@dataclass(frozen=True)
class ProviderResult:
    changed: bool
    status: dict[str, Any]
    conditions: list[dict[str, Any]]
    message: str


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    ca_file = os.environ.get("HQ_CONTROLLER_CA_FILE", "").strip()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise ProviderError("Controller CA bundle could not be loaded.") from exc
    return context


def _condition(
    condition_type: str, status: bool, reason: str, message: str
) -> dict[str, Any]:
    return {
        "type": condition_type,
        "status": status,
        "reason": reason,
        "message": message,
    }


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URLs are deployment config.
            request, timeout=15, context=_tls_context()
        ) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"Provider request failed: {type(exc).__name__}.") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("Provider returned invalid JSON.") from exc


def _required(prefix: str, name: str) -> str:
    value = os.environ.get(f"{prefix}_{name}", "").strip()
    if not value:
        raise ProviderError(f"{prefix}_{name} is required.")
    return value


def _adguard_headers() -> dict[str, str]:
    encoded = base64.b64encode(
        f"{_required('ADGUARD', 'USERNAME')}:{_required('ADGUARD', 'PASSWORD')}".encode()
    ).decode()
    return {"Authorization": f"Basic {encoded}"}


def reconcile_adguard(spec: dict[str, Any], *, apply: bool = True) -> ProviderResult:
    base_url = _required("ADGUARD", "URL").rstrip("/")
    headers = _adguard_headers()
    rewrites = _request(f"{base_url}/control/rewrite/list", headers=headers)
    desired = {"domain": spec["domain"], "answer": spec["answer"]}
    matches = [item for item in rewrites if item.get("domain") == spec["domain"]]
    if len(matches) > 1:
        raise ProviderError("AdGuard contains duplicate rewrites for the domain.")
    if len(matches) == 1 and all(
        matches[0].get(key) == value for key, value in desired.items()
    ):
        changed = False
    elif matches:
        if apply:
            _request(
                f"{base_url}/control/rewrite/update",
                method="PUT",
                headers=headers,
                payload={
                    "target": {
                        "domain": matches[0]["domain"],
                        "answer": matches[0]["answer"],
                    },
                    "update": desired,
                },
            )
        changed = True
    else:
        if apply:
            _request(
                f"{base_url}/control/rewrite/add",
                method="POST",
                headers=headers,
                payload=desired,
            )
        changed = True
    return ProviderResult(
        changed=changed,
        status=desired,
        conditions=[
            _condition("Ready", True, "Reconciled", "AdGuard rewrite is current.")
        ],
        message="AdGuard rewrite updated." if changed else "AdGuard rewrite unchanged.",
    )


def _npm_token(base_url: str) -> str:
    result = _request(
        f"{base_url}/tokens",
        method="POST",
        payload={
            "identity": _required("NPM", "USERNAME"),
            "secret": _required("NPM", "PASSWORD"),
        },
    )
    token = result.get("token", "") if isinstance(result, dict) else ""
    if not token:
        raise ProviderError("NPM authentication did not return a token.")
    return token


def _npm_api_url(configured_url: str) -> str:
    parsed = urllib.parse.urlsplit(configured_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path = f"{path}/api"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def preflight() -> list[dict[str, Any]]:
    adguard_url = _required("ADGUARD", "URL").rstrip("/")
    _request(
        f"{adguard_url}/control/status",
        headers=_adguard_headers(),
    )
    npm_url = _npm_api_url(_required("NPM", "URL"))
    _npm_token(npm_url)
    cloudflare_url = _required("CLOUDFLARE_DNS", "URL").rstrip("/")
    cloudflare_headers = {
        "Authorization": f"Bearer {_required('CLOUDFLARE_DNS', 'API_TOKEN')}"
    }
    verification = _request(
        f"{cloudflare_url}/user/tokens/verify",
        headers=cloudflare_headers,
    )
    if not isinstance(verification, dict) or not verification.get("success"):
        raise ProviderError("Cloudflare DNS token verification failed.")
    zones = _request(
        f"{cloudflare_url}/zones?per_page=50",
        headers=cloudflare_headers,
    )
    available_zones = {
        zone.get("name")
        for zone in zones.get("result", [])
        if isinstance(zone, dict)
    }
    required_zones = {
        "jseverino.com",
        "jseverino.net",
        "jseverino.org",
        "joeseverino.com",
    }
    missing_zones = sorted(required_zones - available_zones)
    if missing_zones:
        raise ProviderError(
            "Cloudflare DNS token cannot read required zones: "
            + ", ".join(missing_zones)
            + "."
        )
    return [
        {
            "connection_ref": _required("ADGUARD", "CONNECTION_REF"),
            "provider": "adguard",
            "ok": True,
        },
        {
            "connection_ref": _required("NPM", "CONNECTION_REF"),
            "provider": "npm",
            "ok": True,
        },
        {
            "connection_ref": _required("CLOUDFLARE_DNS", "CONNECTION_REF"),
            "provider": "cloudflare_dns",
            "ok": True,
        },
    ]


def reconcile_npm(spec: dict[str, Any], *, apply: bool = True) -> ProviderResult:
    base_url = _npm_api_url(_required("NPM", "URL"))
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    domains = sorted(spec["domain_names"])
    matches = [
        host for host in hosts if sorted(host.get("domain_names", [])) == domains
    ]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate proxy hosts for the domain set.")

    desired = {
        "domain_names": domains,
        "forward_scheme": spec["forward_scheme"],
        "forward_host": spec["forward_host"],
        "forward_port": spec["forward_port"],
        "caching_enabled": spec["caching_enabled"],
        "block_exploits": spec["block_exploits"],
        "allow_websocket_upgrade": spec["websocket"],
        "access_list_id": spec["access_list_id"],
        "certificate_id": spec.get("certificate_id") or 0,
        "ssl_forced": spec["force_ssl"],
        "http2_support": spec["http2"],
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "advanced_config": spec["advanced_config"],
        "locations": [],
        "enabled": True,
        "meta": {},
    }
    if matches:
        current = matches[0]
        if spec["force_ssl"] and not current.get("certificate_id"):
            raise ProviderError(
                "NPM host requires TLS but has no certificate; attach the managed "
                "certificate before reconciliation."
            )
        if not desired["certificate_id"]:
            desired["certificate_id"] = current.get("certificate_id", 0)
        desired["locations"] = current.get("locations", [])
        desired["meta"] = current.get("meta", {})
        comparable = {key: current.get(key) for key in desired}
        if comparable == desired:
            changed = False
        else:
            if apply:
                _request(
                    f"{base_url}/nginx/proxy-hosts/{current['id']}",
                    method="PUT",
                    headers=headers,
                    payload=desired,
                )
            changed = True
    else:
        if spec["force_ssl"] and not desired["certificate_id"]:
            raise ProviderError(
                "Creating an HTTPS NPM host requires a resolved certificate ID."
            )
        if apply:
            _request(
                f"{base_url}/nginx/proxy-hosts",
                method="POST",
                headers=headers,
                payload=desired,
            )
        changed = True
    return ProviderResult(
        changed=changed,
        status={
            "domain_names": domains,
            "forward": (
                f"{spec['forward_scheme']}://{spec['forward_host']}:"
                f"{spec['forward_port']}"
            ),
        },
        conditions=[
            _condition("Ready", True, "Reconciled", "NPM proxy host is current.")
        ],
        message="NPM proxy host updated." if changed else "NPM proxy host unchanged.",
    )


def _observe_tls_domain(domain: str) -> dict[str, Any]:
    try:
        tls_context = _tls_context()
        with socket.create_connection((domain, 443), timeout=15) as raw_socket:
            with tls_context.wrap_socket(
                raw_socket, server_hostname=domain
            ) as tls_socket:
                der = tls_socket.getpeercert(binary_form=True)
                certificate = tls_socket.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        raise ProviderError(
            f"TLS observation failed for {domain}: {type(exc).__name__}."
        ) from exc
    if not der or not certificate:
        raise ProviderError(f"TLS observation returned no certificate for {domain}.")
    try:
        expiry = datetime.strptime(
            certificate["notAfter"], "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=timezone.utc)
    except (KeyError, ValueError) as exc:
        raise ProviderError(f"TLS expiry was invalid for {domain}.") from exc
    issuer = {
        key: value
        for relative_name in certificate.get("issuer", ())
        for key, value in relative_name
    }
    return {
        "domain": domain,
        "not_after": expiry.isoformat(),
        "fingerprint_sha256": hashlib.sha256(der).hexdigest(),
        "issuer": issuer.get("organizationName")
        or issuer.get("commonName")
        or "Unknown",
        "sans": sorted(
            value
            for name_type, value in certificate.get("subjectAltName", ())
            if name_type == "DNS"
        ),
        "certificate_pem": ssl.DER_cert_to_PEM_cert(der),
    }


def reconcile_tls(spec: dict[str, Any]) -> ProviderResult:
    observations: list[dict[str, Any]] = []
    direct_observations: list[dict[str, Any]] = []
    direct_fingerprints: set[str] = set()
    unverified_consumers: list[str] = []
    for consumer in spec["consumers"]:
        domains = consumer.get("verify_domains", [])
        if not domains:
            unverified_consumers.append(consumer["name"])
            continue
        for domain in domains:
            observed = _observe_tls_domain(domain)
            observed["consumer"] = consumer["name"]
            observed["consumer_kind"] = consumer["kind"]
            observations.append(observed)
            if consumer["kind"] in {"npm", "caddy"}:
                direct_fingerprints.add(observed["fingerprint_sha256"])
                direct_observations.append(observed)

    if not observations:
        raise ProviderError("No TLS verification domains were declared.")
    managed_observations = direct_observations or observations
    expiries = [
        datetime.fromisoformat(item["not_after"]) for item in managed_observations
    ]
    soonest = min(expiries)
    newest = max(managed_observations, key=lambda item: item["not_after"])
    days_remaining = int((soonest - datetime.now(timezone.utc)).total_seconds() / 86400)
    conditions: list[dict[str, Any]] = []
    if len(direct_fingerprints) > 1:
        conditions.append(
            _condition(
                "Drifted",
                True,
                "ConsumerMismatch",
                "Direct TLS consumers are serving different certificates.",
            )
        )
    if days_remaining <= spec["renewal_window_days"]:
        conditions.append(
            _condition(
                "Degraded",
                True,
                "ExpiringSoon",
                f"A verified TLS consumer expires in {days_remaining} days.",
            )
        )
    if unverified_consumers:
        conditions.append(
            _condition(
                "Degraded",
                True,
                "ConsumerUnverified",
                "No verification domain is declared for: "
                + ", ".join(unverified_consumers),
            )
        )
    if not conditions:
        conditions.append(
            _condition("Ready", True, "Verified", "All TLS consumers are current.")
        )
    public_observations = [
        {key: value for key, value in item.items() if key != "certificate_pem"}
        for item in observations
    ]
    return ProviderResult(
        changed=False,
        status={
            "issuer": newest["issuer"],
            "not_after": soonest.isoformat(),
            "artifact_not_after": newest["not_after"],
            "certificate_pem": newest["certificate_pem"],
            "verified_domains": sorted(item["domain"] for item in observations),
            "consumers": public_observations,
        },
        conditions=conditions,
        message="TLS consumers observed.",
    )


def execute(
    resource: dict[str, Any], action: str, *, apply: bool = True
) -> ProviderResult:
    kind = resource["kind"]
    if kind == "adguard.rewrite" and action == "reconcile":
        return reconcile_adguard(resource["spec"], apply=apply)
    if kind == "npm.proxy_host" and action == "reconcile":
        return reconcile_npm(resource["spec"], apply=apply)
    if kind == "cloudflare.dns_record":
        raise ProviderError("Public DNS mutation is not enabled in this controller.")
    if kind == "tls.certificate" and action == "reconcile":
        return reconcile_tls(resource["spec"])
    if kind == "tls.certificate":
        raise ProviderError("TLS renewal is not enabled.")
    raise ProviderError(f"Unsupported provider/action: {kind}/{action}.")
