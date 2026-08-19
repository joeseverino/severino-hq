"""Fail-closed provider adapters used only by the host-side controller."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
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

from control_plane.providers import certificate_covers


class ProviderError(RuntimeError):
    """A provider operation failed without exposing credential material."""

    def __init__(self, message: str, *, status: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status or {}


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


def _multipart_request(
    url: str,
    *,
    headers: dict[str, str],
    files: dict[str, tuple[str, bytes]],
) -> Any:
    boundary = f"----severino-hq-{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for field, (filename, content) in files.items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: application/x-pem-file\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        url,
        data=b"".join(chunks),
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is deployment config.
            request, timeout=30, context=_tls_context()
        ) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(
            f"Provider multipart request failed: {type(exc).__name__}."
        ) from exc
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
    # AdGuard reports whether a rewrite is switched on, and HQ does not set it:
    # the add and update payloads carry only domain and answer, so claiming to
    # manage it would mean asserting a field this code never sends. It is
    # observed and reported instead -- a rewrite that exists but is switched off
    # does not resolve, and reporting that as Ready was HQ stating something
    # untrue about the world rather than merely knowing less than it could.
    live = matches[0] if matches else {}
    switched_off = live.get("enabled") is False
    status = {**desired, "enabled": live.get("enabled", True)}
    if switched_off:
        return ProviderResult(
            changed=changed,
            status=status,
            conditions=[
                _condition(
                    "Degraded",
                    True,
                    "Disabled",
                    "The rewrite exists in AdGuard but is switched off, so the "
                    "name does not resolve. Re-enable it in AdGuard.",
                )
            ],
            message="AdGuard rewrite is present but disabled.",
        )
    return ProviderResult(
        changed=changed,
        status=status,
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
        # Read from the spec, not asserted. This payload replaces the whole
        # object, so a constant here is not "leave it alone" -- it is "set it to
        # this", every pass, whatever the operator did in NPM.
        "hsts_enabled": spec["hsts_enabled"],
        "hsts_subdomains": spec["hsts_subdomains"],
        "trust_forwarded_proto": spec["trust_forwarded_proto"],
        "advanced_config": spec["advanced_config"],
        "locations": [],
        "enabled": spec["serving"],
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


def _observe_tls_domain(domain: str, *, connect_host: str | None = None) -> dict[str, Any]:
    try:
        tls_context = _tls_context()
        with socket.create_connection((connect_host or domain, 443), timeout=15) as raw_socket:
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


def _consumer_tls_endpoint(
    consumer: dict[str, Any], registry: dict[str, Any]
) -> str | None:
    """Resolve a managed consumer's origin without changing TLS SNI."""
    kind = consumer["kind"]
    if kind == "npm":
        hostname = urllib.parse.urlsplit(_required("NPM", "URL")).hostname
        if not hostname:
            raise ProviderError("NPM origin verification endpoint is missing.")
        return hostname
    if kind in {"caddy", "cpanel"}:
        transport = registry.get("ssh_transports", {}).get(
            consumer["connection_ref"], {}
        )
        hostname = transport.get("host")
        if not hostname:
            raise ProviderError(
                f"{kind} origin verification endpoint is missing."
            )
        return hostname
    return None


def _npm_covered_hosts(certificate_domains: list[str]) -> list[dict[str, Any]]:
    base_url = _npm_api_url(_required("NPM", "URL"))
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    names = set(certificate_domains)
    return [
        host
        for host in hosts
        if host.get("enabled") is not False
        and any(
            certificate_covers(domain, names)
            for domain in host.get("domain_names", [])
        )
    ]


def reconcile_tls(spec: dict[str, Any]) -> ProviderResult:
    observations: list[dict[str, Any]] = []
    consumer_fingerprints: set[str] = set()
    unverified_consumers: list[str] = []
    registry = _certificate_registry("controller-connections.json")
    for consumer in spec["consumers"]:
        domains = list(consumer.get("verify_domains", []))
        if consumer["kind"] == "npm" and consumer.get("discover_covered_hosts"):
            domains = sorted(
                {
                    *domains,
                    *(
                        domain
                        for host in _npm_covered_hosts(spec["domains"])
                        for domain in host.get("domain_names", [])
                    ),
                }
            )
        if not domains:
            unverified_consumers.append(consumer["name"])
            continue
        connect_host = _consumer_tls_endpoint(consumer, registry)
        for domain in domains:
            observed = _observe_tls_domain(domain, connect_host=connect_host)
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


def _resumable_lineage(
    spec: dict[str, Any], deployed_fingerprint: str
) -> tuple[bytes, bytes] | None:
    """Reuse a newer failed-transaction artifact instead of issuing again."""
    lineage = (
        Path(_required("HQ", "ACME_DIR"))
        / "config"
        / "live"
        / spec["certificate_name"]
    )
    try:
        fullchain = lineage.joinpath("fullchain.pem").read_bytes()
        private_key = lineage.joinpath("privkey.pem").read_bytes()
    except OSError:
        return None
    fingerprint = _validate_certificate(fullchain, private_key, spec["domains"])
    if fingerprint == deployed_fingerprint:
        return None
    with tempfile.TemporaryDirectory() as directory:
        cert_path = Path(directory) / "fullchain.pem"
        cert_path.write_bytes(fullchain)
        raw_expiry = _run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"]
        ).decode().strip()
    try:
        expiry = datetime.strptime(
            raw_expiry.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProviderError("Certbot lineage expiry is invalid.") from exc
    minimum_expiry = datetime.now(timezone.utc) + timedelta(
        days=spec["renewal_window_days"]
    )
    if expiry <= minimum_expiry:
        return None
    return fullchain, private_key


def _npm_managed_certificate(
    consumer: dict[str, Any],
    certificate_domains: list[str],
    fullchain: bytes,
    private_key: bytes,
) -> tuple[int, dict[str, str]]:
    base_url = _npm_api_url(_required("NPM", "URL"))
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    nice_name = f"Severino HQ - {consumer['name']}"
    certificates = _request(f"{base_url}/nginx/certificates", headers=headers)
    matches = [item for item in certificates if item.get("nice_name") == nice_name]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate HQ-managed certificates.")
    if matches:
        certificate = matches[0]
        if certificate.get("provider") != "other":
            raise ProviderError("The HQ-managed NPM certificate is not a custom certificate.")
    else:
        certificate = _request(
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
    _multipart_request(
        f"{base_url}/nginx/certificates/validate",
        headers=headers,
        files=files,
    )
    _multipart_request(
        f"{base_url}/nginx/certificates/{certificate_id}/upload",
        headers=headers,
        files=files,
    )
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
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
        _request(
            f"{base_url}/nginx/proxy-hosts/{host['id']}",
            method="PUT",
            headers=headers,
            payload={"certificate_id": certificate_id},
        )
    return certificate_id, {"nice_name": nice_name}


def _deploy_certificate(
    spec: dict[str, Any], fullchain: bytes, private_key: bytes
) -> dict[str, Any]:
    deployment_status: dict[str, Any] = {}
    bundle = _certificate_bundle(fullchain, private_key)
    marker = b"-----END CERTIFICATE-----"
    leaf_body, separator, chain_body = fullchain.partition(marker)
    if not separator:
        raise ProviderError("Certificate chain does not contain a leaf certificate.")
    leaf = leaf_body + marker + b"\n"
    chain = chain_body.lstrip()
    for consumer in spec["consumers"]:
        try:
            if consumer["kind"] == "npm":
                certificate_id, identity = _npm_managed_certificate(
                    consumer, spec["domains"], fullchain, private_key
                )
                deployment_status.update(
                    npm_certificate_id=certificate_id,
                    npm_certificate_identity=identity,
                )
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
        except ProviderError as exc:
            raise ProviderError(
                f"TLS deployment failed for {consumer['name']} "
                f"({consumer['kind']}): {exc}"
            ) from exc
    return deployment_status


def _tls_verification_policy() -> tuple[int, int]:
    registry = _certificate_registry("controller-capabilities.json")
    policy = (
        registry.get("capabilities", {})
        .get("tls.certificate", {})
        .get("actions", {})
        .get("renew", {})
        .get("verification", {})
    )
    timeout = policy.get("timeout_seconds")
    interval = policy.get("interval_seconds")
    if not isinstance(timeout, int) or not isinstance(interval, int):
        raise ProviderError("TLS renewal verification policy is invalid.")
    if not 30 <= timeout <= 600 or not 1 <= interval <= 30 or interval > timeout:
        raise ProviderError("TLS renewal verification policy is out of bounds.")
    return timeout, interval


def _verify_tls_deployment(
    spec: dict[str, Any], expected_fingerprint: str
) -> ProviderResult:
    timeout, interval = _tls_verification_policy()
    deadline = time.monotonic() + timeout
    while True:
        result = reconcile_tls(spec)
        fingerprints = {
            item["fingerprint_sha256"] for item in result.status["consumers"]
        }
        if fingerprints == {expected_fingerprint}:
            return result
        if time.monotonic() >= deadline:
            evidence = [
                {
                    "consumer": item["consumer"],
                    "kind": item["consumer_kind"],
                    "domain": item["domain"],
                    "fingerprint_sha256": item["fingerprint_sha256"],
                    "matches_expected": (
                        item["fingerprint_sha256"] == expected_fingerprint
                    ),
                }
                for item in result.status["consumers"]
            ]
            failed = {
                item["consumer"]
                for item in evidence
                if not item["matches_expected"]
            }
            raise ProviderError(
                f"{len(failed)} of {len(spec['consumers'])} TLS consumers "
                "did not activate the certificate in time.",
                status={
                    "expected_fingerprint_sha256": expected_fingerprint,
                    "consumers": evidence,
                },
            )
        time.sleep(interval)


def _tls_match_evidence(
    status: dict[str, Any], expected_fingerprint: str
) -> dict[str, Any]:
    """Attach the canonical expected fingerprint and verdict to every observation."""
    return {
        **status,
        "expected_fingerprint_sha256": expected_fingerprint,
        "consumers": [
            {
                **item,
                "matches_expected": (
                    item["fingerprint_sha256"] == expected_fingerprint
                ),
            }
            for item in status["consumers"]
        ],
    }


def _deploy_tls_transaction(
    spec: dict[str, Any],
    fullchain: bytes,
    private_key: bytes,
    previous_fullchain: bytes,
    previous_key: bytes,
    *,
    artifact_source: str,
    reason: str,
    message: str,
) -> ProviderResult:
    expected_fingerprint = _validate_certificate(fullchain, private_key, spec["domains"])
    try:
        deployment_status = _deploy_certificate(spec, fullchain, private_key)
        observed = _verify_tls_deployment(spec, expected_fingerprint)
    except ProviderError as exc:
        try:
            _deploy_certificate(spec, previous_fullchain, previous_key)
        except ProviderError as rollback_exc:
            raise ProviderError(
                f"Certificate deployment failed ({exc}); rollback also failed "
                f"({rollback_exc})."
            ) from rollback_exc
        raise ProviderError(
            f"Certificate deployment failed: {exc} Rollback succeeded.",
            status=exc.status,
        ) from exc
    status = {
        **_tls_match_evidence(observed.status, expected_fingerprint),
        **deployment_status,
        "artifact_source": artifact_source,
        "renewed_fingerprint_sha256": expected_fingerprint,
    }
    return ProviderResult(
        changed=True,
        status=status,
        conditions=[
            _condition(
                "Ready", True, reason, "All TLS consumers serve the certificate."
            )
        ],
        message=message,
    )


def _lineage(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    lineage = (
        Path(_required("HQ", "ACME_DIR"))
        / "config"
        / "live"
        / spec["certificate_name"]
    )
    try:
        return lineage.joinpath("fullchain.pem").read_bytes(), lineage.joinpath(
            "privkey.pem"
        ).read_bytes()
    except OSError as exc:
        raise ProviderError("Certbot lineage is unavailable for reconciliation.") from exc


def apply_tls_reconcile(spec: dict[str, Any]) -> ProviderResult:
    fullchain, private_key = _lineage(spec)
    expected = _validate_certificate(fullchain, private_key, spec["domains"])
    observed = reconcile_tls(spec)
    fingerprints = {
        item["fingerprint_sha256"] for item in observed.status["consumers"]
    }
    if fingerprints == {expected}:
        return ProviderResult(
            changed=False,
            status={
                **_tls_match_evidence(observed.status, expected),
                "artifact_source": "existing_lineage",
            },
            conditions=[
                _condition("Ready", True, "Verified", "All TLS consumers match.")
            ],
            message="Certificate consumers already match the managed lineage.",
        )
    caddy = next(
        (item for item in spec["consumers"] if item["kind"] == "caddy"), None
    )
    if caddy is None:
        raise ProviderError("Certificate reconciliation requires a rollback source.")
    previous_fullchain, previous_key = _read_bundle(
        _ssh(caddy["connection_ref"], "snapshot")
    )
    return _deploy_tls_transaction(
        spec,
        fullchain,
        private_key,
        previous_fullchain,
        previous_key,
        artifact_source="existing_lineage",
        reason="Reconciled",
        message="Certificate redistributed and verified without issuance.",
    )


def renew_tls(spec: dict[str, Any]) -> ProviderResult:
    caddy = next(
        (item for item in spec["consumers"] if item["kind"] == "caddy"), None
    )
    if caddy is None:
        raise ProviderError("Certificate renewal requires a rollback source.")
    previous_fullchain, previous_key = _read_bundle(
        _ssh(caddy["connection_ref"], "snapshot")
    )
    previous_fingerprint = _validate_certificate(
        previous_fullchain, previous_key, spec["domains"]
    )
    resumed = _resumable_lineage(spec, previous_fingerprint)
    if resumed is None:
        fullchain, private_key = _issue_certificate(spec)
        artifact_source = "new_issuance"
    else:
        fullchain, private_key = resumed
        artifact_source = "existing_lineage"
    return _deploy_tls_transaction(
        spec,
        fullchain,
        private_key,
        previous_fullchain,
        previous_key,
        artifact_source=artifact_source,
        reason="Renewed",
        message="Certificate renewed, deployed, and verified.",
    )


def _tls_reconcile(spec: dict[str, Any], *, apply: bool) -> ProviderResult:
    return apply_tls_reconcile(spec) if apply else reconcile_tls(spec)


def _tls_renew(spec: dict[str, Any], *, apply: bool) -> ProviderResult:
    if apply:
        return renew_tls(spec)
    return ProviderResult(
        changed=True,
        status={},
        conditions=[],
        message="Certificate would be issued, deployed, verified, and rolled back on failure.",
    )


def _public_dns_locked(spec: dict[str, Any], *, apply: bool) -> ProviderResult:
    del spec, apply
    raise ProviderError("Public DNS mutation is not enabled in this controller.")


def delete_adguard(spec: dict[str, Any], *, apply: bool = True) -> ProviderResult:
    """Remove the rewrite, and treat an already-absent one as success.

    Deletion has to be idempotent because the operation queue is: a delete that
    applied and then failed to report is retried, and a second attempt finding
    nothing there has achieved exactly what was asked.
    """

    base_url = _required("ADGUARD", "URL").rstrip("/")
    headers = _adguard_headers()
    rewrites = _request(f"{base_url}/control/rewrite/list", headers=headers)
    matches = [item for item in rewrites if item.get("domain") == spec["domain"]]
    if not matches:
        return ProviderResult(
            changed=False,
            status={"domain": spec["domain"], "removed": True},
            conditions=[
                _condition("Ready", True, "Absent", "No such rewrite in AdGuard.")
            ],
            message="AdGuard rewrite was already absent.",
        )
    if apply:
        for match in matches:
            # The live record, not the desired one: AdGuard identifies a rewrite
            # by the pair, and a spec whose answer has drifted would not match.
            _request(
                f"{base_url}/control/rewrite/delete",
                method="POST",
                headers=headers,
                payload={"domain": match["domain"], "answer": match["answer"]},
            )
    return ProviderResult(
        changed=True,
        status={"domain": spec["domain"], "removed": True},
        conditions=[
            _condition("Ready", True, "Removed", "AdGuard rewrite was removed.")
        ],
        message="AdGuard rewrite removed.",
    )


def delete_npm(spec: dict[str, Any], *, apply: bool = True) -> ProviderResult:
    """Remove the proxy host matching this exact domain set."""

    base_url = _npm_api_url(_required("NPM", "URL"))
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    domains = sorted(spec["domain_names"])
    matches = [
        host for host in hosts if sorted(host.get("domain_names", [])) == domains
    ]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate proxy hosts for the domain set.")
    if not matches:
        return ProviderResult(
            changed=False,
            status={"domain_names": domains, "removed": True},
            conditions=[
                _condition("Ready", True, "Absent", "No such proxy host in NPM.")
            ],
            message="NPM proxy host was already absent.",
        )
    if apply:
        _request(
            f"{base_url}/nginx/proxy-hosts/{matches[0]['id']}",
            method="DELETE",
            headers=headers,
        )
    return ProviderResult(
        changed=True,
        status={"domain_names": domains, "removed": True},
        conditions=[
            _condition("Ready", True, "Removed", "NPM proxy host was removed.")
        ],
        message="NPM proxy host removed.",
    )


def list_adguard() -> list[dict[str, Any]]:
    base_url = _required("ADGUARD", "URL").rstrip("/")
    records = _request(f"{base_url}/control/rewrite/list", headers=_adguard_headers())
    return [
        {
            "domain": item["domain"],
            "answer": item["answer"],
            "enabled": item.get("enabled", True),
        }
        for item in records
        if item.get("domain") and item.get("answer")
    ]


def list_npm() -> list[dict[str, Any]]:
    base_url = _npm_api_url(_required("NPM", "URL"))
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    records = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    # Only the fields HQ can express, plus the identity. The rest is NPM's
    # business, and copying a whole provider object into HQ would make this an
    # inventory of NPM rather than a list of what HQ could manage.
    fields = (
        "domain_names",
        "forward_scheme",
        "forward_host",
        "forward_port",
        "ssl_forced",
        "http2_support",
        "allow_websocket_upgrade",
        "caching_enabled",
        "block_exploits",
        "access_list_id",
        "advanced_config",
        "hsts_enabled",
        "hsts_subdomains",
        "trust_forwarded_proto",
        "enabled",
    )
    return [
        {field: record.get(field) for field in fields}
        for record in records
        if record.get("domain_names")
    ]


PROVIDER_INVENTORY = {
    "adguard.rewrite": list_adguard,
    "npm.proxy_host": list_npm,
}


def inventory() -> dict[str, Any]:
    """Everything each provider holds, whether or not HQ declared it.

    The reconcilers already fetch these lists in full and keep only the one
    record they were asked about. Reporting the rest costs nothing extra at the
    provider and is the difference between HQ knowing about the resources it
    created and HQ knowing what is actually out there.

    One unreachable provider reports as unreachable rather than failing the
    sweep. Losing the whole inventory because a single service is restarting
    would make the least reliable provider decide whether HQ can see any of them.
    """

    found: dict[str, Any] = {}
    for kind, lister in PROVIDER_INVENTORY.items():
        try:
            found[kind] = {"ok": True, "records": lister()}
        except (ProviderError, OSError, ValueError, KeyError) as exc:
            found[kind] = {"ok": False, "records": [], "error": str(exc)}
    return found


PROVIDER_ACTIONS = {
    ("adguard.rewrite", "reconcile"): reconcile_adguard,
    ("adguard.rewrite", "delete"): delete_adguard,
    ("npm.proxy_host", "reconcile"): reconcile_npm,
    ("npm.proxy_host", "delete"): delete_npm,
    ("cloudflare.dns_record", "reconcile"): _public_dns_locked,
    ("tls.certificate", "reconcile"): _tls_reconcile,
    ("tls.certificate", "renew"): _tls_renew,
}


def execute(
    resource: dict[str, Any], action: str, *, apply: bool = True
) -> ProviderResult:
    identity = (resource["kind"], action)
    try:
        handler = PROVIDER_ACTIONS[identity]
    except KeyError as exc:
        raise ProviderError(
            f"Unsupported provider/action: {identity[0]}/{identity[1]}."
        ) from exc
    return handler(resource["spec"], apply=apply)
