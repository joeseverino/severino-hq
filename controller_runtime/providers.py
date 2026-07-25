"""Fail-closed provider adapters used only by the host-side controller."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
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
    acme_dir = Path(_required("HQ", "ACME_DIR"))
    if not acme_dir.is_dir() or not os.access(acme_dir, os.W_OK):
        raise ProviderError("ACME state directory is not writable.")
    _run(["certbot", "--version"])
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
    _ssh("edge", "preflight")
    _ssh("namecheap-cpanel", "preflight")
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
        {"connection_ref": "edge", "provider": "ssh", "ok": True},
        {
            "connection_ref": "namecheap-cpanel",
            "provider": "ssh",
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
    consumer_fingerprints: set[str] = set()
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
            consumer_fingerprints.add(observed["fingerprint_sha256"])

    if not observations:
        raise ProviderError("No TLS verification domains were declared.")
    expiries = [datetime.fromisoformat(item["not_after"]) for item in observations]
    soonest = min(expiries)
    newest = max(observations, key=lambda item: item["not_after"])
    days_remaining = int((soonest - datetime.now(timezone.utc)).total_seconds() / 86400)
    conditions: list[dict[str, Any]] = []
    if len(consumer_fingerprints) > 1:
        conditions.append(
            _condition(
                "Drifted",
                True,
                "ConsumerMismatch",
                "TLS consumers are serving different certificates.",
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


def _certificate_registry(name: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "config" / name
    try:
        registry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("Certificate controller registry is invalid.") from exc
    if registry.get("schema_version") != 1:
        raise ProviderError("Certificate controller registry version is unsupported.")
    return registry


def _run(command: list[str], *, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError("Certificate controller command could not complete.") from exc
    if result.returncode:
        raise ProviderError("Certificate controller command failed.")
    return result.stdout


def _ssh(connection_ref: str, operation: str, payload: bytes | None = None) -> bytes:
    registry = _certificate_registry("controller-connections.json")
    transport = registry.get("ssh_transports", {}).get(connection_ref)
    if not isinstance(transport, dict):
        raise ProviderError(f"Unknown certificate transport: {connection_ref}.")
    ssh_dir = Path(_required("HQ_CONTROLLER", "SSH_DIR"))
    command = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={ssh_dir / 'known_hosts'}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(ssh_dir / connection_ref),
        "-p",
        str(transport["port"]),
        f'{transport["user"]}@{transport["host"]}',
        operation,
    ]
    return _run(command, input_bytes=payload)


def _certificate_bundle(fullchain: bytes, private_key: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, value in (("fullchain.pem", fullchain), ("privkey.pem", private_key)):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


def _read_bundle(payload: bytes) -> tuple[bytes, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            names = set(archive.getnames())
            if names != {"fullchain.pem", "privkey.pem"}:
                raise ProviderError("Certificate snapshot contained unexpected files.")
            fullchain_file = archive.extractfile("fullchain.pem")
            private_key_file = archive.extractfile("privkey.pem")
            if fullchain_file is None or private_key_file is None:
                raise ProviderError("Certificate snapshot was incomplete.")
            return fullchain_file.read(), private_key_file.read()
    except (tarfile.TarError, OSError) as exc:
        raise ProviderError("Certificate snapshot was invalid.") from exc


def _validate_certificate(
    fullchain: bytes, private_key: bytes, domains: list[str]
) -> str:
    with tempfile.TemporaryDirectory() as directory:
        cert_path = Path(directory) / "fullchain.pem"
        key_path = Path(directory) / "privkey.pem"
        cert_path.write_bytes(fullchain)
        key_path.write_bytes(private_key)
        cert_pub = _run(["openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout"])
        key_pub = _run(["openssl", "pkey", "-in", str(key_path), "-pubout"])
        if cert_pub != key_pub:
            raise ProviderError("Certificate and private key do not match.")
        fingerprint = _run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256"]
        ).decode().strip().split("=", 1)[-1].replace(":", "").lower()
        san_output = _run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"]
        ).decode()
        sans = {
            chunk.split(",", 1)[0].strip()
            for chunk in san_output.replace("\n", " ").split("DNS:")[1:]
        }
        missing = sorted(set(domains) - sans)
        if missing:
            raise ProviderError(
                "Issued certificate is missing names: " + ", ".join(missing) + "."
            )
        return fingerprint


def _issue_certificate(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    registry = _certificate_registry("controller-certificates.json")["acme"]
    acme_dir = Path(_required("HQ", "ACME_DIR"))
    credentials = acme_dir / "cloudflare.ini"
    credentials.write_text(
        "dns_cloudflare_api_token = "
        + _required("CLOUDFLARE_DNS", "API_TOKEN")
        + "\n"
    )
    credentials.chmod(0o600)
    command = [
        "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--email",
        registry["email"],
        "--server",
        registry["directory_url"],
        "--dns-cloudflare",
        "--dns-cloudflare-credentials",
        str(credentials),
        "--dns-cloudflare-propagation-seconds",
        str(registry["dns_propagation_seconds"]),
        "--config-dir",
        str(acme_dir / "config"),
        "--work-dir",
        str(acme_dir / "work"),
        "--logs-dir",
        str(acme_dir / "logs"),
        "--cert-name",
        spec["certificate_name"],
        "--force-renewal",
    ]
    for domain in spec["domains"]:
        command.extend(("-d", domain))
    try:
        _run(command)
    finally:
        credentials.unlink(missing_ok=True)
    lineage = acme_dir / "config" / "live" / spec["certificate_name"]
    try:
        return (lineage.joinpath("fullchain.pem").read_bytes(), lineage.joinpath("privkey.pem").read_bytes())
    except OSError as exc:
        raise ProviderError("Certbot did not produce a complete lineage.") from exc


def _npm_upload(certificate_id: int, fullchain: bytes, private_key: bytes) -> None:
    base_url = _npm_api_url(_required("NPM", "URL"))
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    payload = {
        "certificate": fullchain.decode(),
        "certificate_key": private_key.decode(),
    }
    _request(
        f"{base_url}/nginx/certificates/validate",
        method="POST",
        headers=headers,
        payload=payload,
    )
    _request(
        f"{base_url}/nginx/certificates/{certificate_id}/upload",
        method="POST",
        headers=headers,
        payload=payload,
    )


def _deploy_certificate(
    spec: dict[str, Any], fullchain: bytes, private_key: bytes
) -> None:
    bundle = _certificate_bundle(fullchain, private_key)
    marker = b"-----END CERTIFICATE-----"
    leaf_body, separator, chain_body = fullchain.partition(marker)
    if not separator:
        raise ProviderError("Certificate chain does not contain a leaf certificate.")
    leaf = leaf_body + marker + b"\n"
    chain = chain_body.lstrip()
    for consumer in spec["consumers"]:
        if consumer["kind"] == "npm":
            _npm_upload(consumer["certificate_id"], fullchain, private_key)
        elif consumer["kind"] == "caddy":
            _ssh(consumer["connection_ref"], "deploy", bundle)
        elif consumer["kind"] == "cpanel":
            for domain in consumer["install_domains"]:
                payload = json.dumps(
                    {
                        "domain": domain,
                        "cert": leaf.decode(),
                        "key": private_key.decode(),
                        "cabundle": chain.decode(),
                    },
                    separators=(",", ":"),
                ).encode()
                _ssh(consumer["connection_ref"], f"deploy:{domain}", payload)


def renew_tls(spec: dict[str, Any]) -> ProviderResult:
    caddy = next(
        (item for item in spec["consumers"] if item["kind"] == "caddy"), None
    )
    if caddy is None:
        raise ProviderError("Certificate renewal requires a rollback source.")
    previous_fullchain, previous_key = _read_bundle(
        _ssh(caddy["connection_ref"], "snapshot")
    )
    _validate_certificate(previous_fullchain, previous_key, spec["domains"])
    fullchain, private_key = _issue_certificate(spec)
    expected_fingerprint = _validate_certificate(fullchain, private_key, spec["domains"])
    try:
        _deploy_certificate(spec, fullchain, private_key)
        last_result = None
        for _ in range(12):
            last_result = reconcile_tls(spec)
            fingerprints = {
                item["fingerprint_sha256"]
                for item in last_result.status["consumers"]
            }
            if fingerprints == {expected_fingerprint}:
                break
            time.sleep(5)
        else:
            raise ProviderError("Consumers did not serve the renewed certificate.")
    except ProviderError as exc:
        try:
            _deploy_certificate(spec, previous_fullchain, previous_key)
        except ProviderError as rollback_exc:
            raise ProviderError("Certificate deployment and rollback both failed.") from rollback_exc
        raise ProviderError("Certificate deployment failed and was rolled back.") from exc
    assert last_result is not None
    status = {**last_result.status, "renewed_fingerprint_sha256": expected_fingerprint}
    return ProviderResult(
        changed=True,
        status=status,
        conditions=[_condition("Ready", True, "Renewed", "All TLS consumers serve the renewed certificate.")],
        message="Certificate renewed, deployed, and verified.",
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
    if kind == "tls.certificate" and action == "renew":
        if not apply:
            return ProviderResult(
                changed=True,
                status={},
                conditions=[],
                message="Certificate would be issued, deployed, verified, and rolled back on failure.",
            )
        return renew_tls(resource["spec"])
    raise ProviderError(f"Unsupported provider/action: {kind}/{action}.")
