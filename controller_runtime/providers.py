"""Fail-closed provider adapters used only by the host-side controller."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import logging
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TypeVar, cast

from analytics.contracts import MAX_QUERY_DAYS, completed_window
from control_plane.providers import (
    CERTIFICATE_KIND,
    caa_parts,
    certificate_covers,
    controller_capability_registry,
    controller_id,
    CONTROLLER_PROVIDER_ADAPTERS,
    normalized_record_content,
    normalized_hostname,
)
from control_plane.provider_adapters.contracts import (
    ProviderError,
    ProviderResult,
    compile_controller_adapters,
)


logger = logging.getLogger("severino.controller")

_SnapshotValue = TypeVar("_SnapshotValue")
_PROVIDER_SNAPSHOT: ContextVar[dict[tuple[object, ...], object] | None] = ContextVar(
    "provider_snapshot", default=None
)


@contextmanager
def provider_snapshot() -> Iterator[None]:
    """Share successful reads only for one logically atomic provider sweep."""

    token = _PROVIDER_SNAPSHOT.set({})
    try:
        yield
    finally:
        _PROVIDER_SNAPSHOT.reset(token)


def _snapshot_value(
    key: tuple[object, ...], load: Callable[[], _SnapshotValue]
) -> _SnapshotValue:
    snapshot = _PROVIDER_SNAPSHOT.get()
    if snapshot is None:
        return load()
    if key not in snapshot:
        snapshot[key] = load()
    return cast(_SnapshotValue, snapshot[key])


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


def _npm_url(connection_ref: str = "") -> str:
    return _npm_api_url(_required(connection_prefix("npm", connection_ref), "URL"))


def _npm_token(base_url: str, connection_ref: str = "") -> str:
    prefix = connection_prefix("npm", connection_ref)

    def exchange() -> str:
        result = _request(
            f"{base_url}/tokens",
            method="POST",
            payload={
                "identity": _required(prefix, "USERNAME"),
                "secret": _required(prefix, "PASSWORD"),
            },
        )
        token = result.get("token", "") if isinstance(result, dict) else ""
        if not token:
            raise ProviderError("NPM authentication did not return a token.")
        return token

    return _snapshot_value(("npm-token", base_url, prefix), exchange)


def _npm_api_url(configured_url: str) -> str:
    parsed = urllib.parse.urlsplit(configured_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path = f"{path}/api"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def reconcile_npm(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    domains = sorted(spec["domain_names"])
    matches = [
        host for host in hosts if sorted(host.get("domain_names", [])) == domains
    ]
    if not matches:
        # A renamed proxy host: the one that exists still answers to the names
        # HQ last saw, and NPM updates it in place by id.
        previous = sorted((observed or {}).get("domain_names") or ())
        if previous and previous != domains:
            matches = [
                host
                for host in hosts
                if sorted(host.get("domain_names", [])) == previous
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


def _observe_tls_domain(
    domain: str, *, connect_host: str | None = None
) -> dict[str, Any]:
    try:
        tls_context = _tls_context()
        with socket.create_connection(
            (connect_host or domain, 443), timeout=15
        ) as raw_socket:
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


def _consumer_tls_endpoint(consumer: dict[str, Any]) -> str | None:
    """Resolve a managed consumer's origin without changing TLS SNI."""
    kind = consumer["kind"]
    if kind == "npm":
        hostname = urllib.parse.urlsplit(
            _required(connection_prefix("npm"), "URL")
        ).hostname
        if not hostname:
            raise ProviderError("NPM origin verification endpoint is missing.")
        return hostname
    if kind in {"caddy", "cpanel"}:
        transport = _transport(consumer["connection_ref"])
        hostname = transport.get("host")
        if not hostname:
            raise ProviderError(f"{kind} origin verification endpoint is missing.")
        return hostname
    return None


def _npm_covered_hosts(certificate_domains: list[str]) -> list[dict[str, Any]]:
    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    names = set(certificate_domains)
    return [
        host
        for host in hosts
        if host.get("enabled") is not False
        and any(
            certificate_covers(domain, names) for domain in host.get("domain_names", [])
        )
    ]


def reconcile_tls(spec: dict[str, Any]) -> ProviderResult:
    observations: list[dict[str, Any]] = []
    consumer_fingerprints: set[str] = set()
    unverified_consumers: list[str] = []
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
        connect_host = _consumer_tls_endpoint(consumer)
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


def connection_prefixes() -> dict[str, str]:
    """Every connection the environment carries, as ref -> env prefix.

    The rendered environment is the inventory. `render-controller-env.sh`
    resolves each 1Password item into `<PREFIX>_CONNECTION_REF` alongside that
    connection's values, so what the controller can reach is exactly what it was
    given credentials for -- nothing here has to be told separately.
    """

    suffix = "_CONNECTION_REF"
    return {
        value: name[: -len(suffix)]
        for name, value in os.environ.items()
        if name.endswith(suffix) and value
    }


def connection_provider(connection_ref: str) -> str:
    """What kind of thing one connection reaches.

    The env prefix is the answer unless the 1Password item says otherwise, which
    makes the long-standing convention -- `ADGUARD_URL` is AdGuard's URL -- the
    rule rather than a coincidence every provider had to restate. Declaring it
    on the item is what lets two of the same kind coexist: `PORTAINER_HOME` and
    `PORTAINER_CLOUD` are both portainer, and neither has to be named here.
    """

    prefix = connection_prefixes().get(connection_ref, "")
    if not prefix:
        return ""
    return os.environ.get(f"{prefix}_PROVIDER", "").strip() or prefix.lower()


def connection_prefix(provider: str, connection_ref: str = "") -> str:
    """The env prefix for one connection: the one named, or the only one.

    A provider that takes a ``connection_ref`` resolves it here and reaches
    exactly that endpoint, so a second Portainer is a second 1Password item and
    nothing more. A provider whose spec names none gets the sole connection for
    its kind; two of them is an error rather than a silent choice between them,
    because guessing would reconcile the wrong estate.

    Falls back to the provider's own name in upper case, which is the prefix a
    deployment that has not yet labelled its connections is already using.
    """

    inventory = connection_prefixes()
    if connection_ref:
        prefix = inventory.get(connection_ref)
        if not prefix:
            raise ProviderError(
                f"No connection named {connection_ref!r} was supplied to the "
                "controller."
            )
        return prefix
    candidates = sorted(
        prefix
        for ref, prefix in inventory.items()
        if connection_provider(ref) == provider
    )
    if len(candidates) > 1:
        raise ProviderError(
            f"More than one connection is a {provider}; the resource has to say which."
        )
    return candidates[0] if candidates else provider.upper()


def provider_connection_refs(provider: str) -> tuple[str, ...]:
    """Every connection that is one of these, as its own item declares.

    What a sweep iterates. Two Portainers are two 1Password items and get swept
    as two, so an estate grows by being given a credential rather than by being
    named anywhere. Falls back to the conventional prefix for a deployment whose
    items do not carry the field yet, which is one connection, the one it has.
    """

    declared = tuple(
        sorted(
            ref for ref in connection_prefixes() if connection_provider(ref) == provider
        )
    )
    if declared:
        return declared
    conventional = os.environ.get(f"{provider.upper()}_CONNECTION_REF", "").strip()
    return (conventional,) if conventional else ()


def ssh_connection_refs() -> tuple[str, ...]:
    """Connections rendered through the ssh_transport projection.

    Identified by the values that projection produces rather than by a declared
    kind: a connection carrying a host and a user is one this can open.
    """

    return tuple(
        sorted(
            ref
            for ref, prefix in connection_prefixes().items()
            if os.environ.get(f"{prefix}_HOST") and os.environ.get(f"{prefix}_USER")
        )
    )


def _transport(connection_ref: str) -> dict[str, Any]:
    """The endpoint for an SSH connection, from the rendered environment."""

    prefix = connection_prefixes().get(connection_ref)
    if not prefix or not os.environ.get(f"{prefix}_HOST"):
        raise ProviderError(f"Unknown certificate transport: {connection_ref}.")
    port = _required(prefix, "PORT")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ProviderError(f"{prefix}_PORT is not a port number.")
    return {
        "host": _required(prefix, "HOST"),
        "port": int(port),
        "user": _required(prefix, "USER"),
        "host_key": _required(prefix, "HOST_KEY"),
    }


def controller_config_dir() -> Path:
    """Where this deployment's controller registries live.

    `SEVERINO_CONTROLLER_CONFIG_DIR` overrides the copy in the repository, so a
    deployment's hosts, connections and ACME identity are supplied at runtime
    rather than committed.
    """

    override = os.environ.get("SEVERINO_CONTROLLER_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "config"


def _run(
    command: list[str], *, input_bytes: bytes | None = None, step: str = "command"
) -> bytes:
    """Run a subprocess, saying which step failed rather than which module ran it.

    Every caller here used to report the same sentence, so a failure anywhere
    -- certbot, openssl, or an SSH call to a host -- surfaced as "Certificate
    controller command failed." A DNS reconcile that never touches a
    certificate reported a certificate error, and the search started in the
    wrong place. `step` names the thing that actually failed.

    The subprocess's own output is logged, never returned: it carries paths and
    remote messages that belong in an operator's log rather than in a provider
    result that reaches an API client.
    """

    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(f"{step} could not complete.") from exc
    if result.returncode:
        logger.warning(
            "controller step failed",
            extra={
                "event": "controller.step.failed",
                "step": step,
                "exit_code": result.returncode,
                "stderr": result.stderr.decode("utf-8", "replace")[:2000],
            },
        )
        raise ProviderError(f"{step} failed.")
    return result.stdout


def _ssh(connection_ref: str, operation: str, payload: bytes | None = None) -> bytes:
    transport = _transport(connection_ref)
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
        f"{transport['user']}@{transport['host']}",
        operation,
    ]
    return _run(
        command, input_bytes=payload, step=f"SSH {operation} for {connection_ref}"
    )


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
        cert_pub = _run(
            ["openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout"],
            step="reading the certificate",
        )
        key_pub = _run(
            ["openssl", "pkey", "-in", str(key_path), "-pubout"],
            step="reading the private key",
        )
        if cert_pub != key_pub:
            raise ProviderError("Certificate and private key do not match.")
        fingerprint = (
            _run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(cert_path),
                    "-noout",
                    "-fingerprint",
                    "-sha256",
                ]
            )
            .decode()
            .strip()
            .split("=", 1)[-1]
            .replace(":", "")
            .lower()
        )
        san_output = _run(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-ext",
                "subjectAltName",
            ]
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


# How long to wait for a DNS-01 challenge record to propagate. A tuning value,
# not a deployment identity, so it has a default and an override rather than a
# place in the vault.
ACME_PROPAGATION_SECONDS = os.environ.get("ACME_PROPAGATION_SECONDS", "30")


def _issue_certificate(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    acme_dir = Path(_required("HQ", "ACME_DIR"))
    if not acme_dir.is_dir() or not os.access(acme_dir, os.W_OK):
        raise ProviderError("ACME state directory is not writable.")
    _run(["certbot", "--version"], step="certbot preflight")
    credentials = acme_dir / "cloudflare.ini"
    credentials.write_text("dns_cloudflare_api_token = " + _cloudflare_token() + "\n")
    credentials.chmod(0o600)
    command = [
        "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--email",
        _required("ACME", "EMAIL"),
        "--server",
        _required("ACME", "DIRECTORY_URL"),
        "--dns-cloudflare",
        "--dns-cloudflare-credentials",
        str(credentials),
        "--dns-cloudflare-propagation-seconds",
        ACME_PROPAGATION_SECONDS,
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
        return (
            lineage.joinpath("fullchain.pem").read_bytes(),
            lineage.joinpath("privkey.pem").read_bytes(),
        )
    except OSError as exc:
        raise ProviderError("Certbot did not produce a complete lineage.") from exc


def _resumable_lineage(
    spec: dict[str, Any], deployed_fingerprint: str
) -> tuple[bytes, bytes] | None:
    """Reuse a newer failed-transaction artifact instead of issuing again."""
    lineage = (
        Path(_required("HQ", "ACME_DIR")) / "config" / "live" / spec["certificate_name"]
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
        raw_expiry = (
            _run(["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"])
            .decode()
            .strip()
        )
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
    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    nice_name = f"Severino HQ - {consumer['name']}"
    certificates = _request(f"{base_url}/nginx/certificates", headers=headers)
    matches = [item for item in certificates if item.get("nice_name") == nice_name]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate HQ-managed certificates.")
    if matches:
        certificate = matches[0]
        if certificate.get("provider") != "other":
            raise ProviderError(
                "The HQ-managed NPM certificate is not a custom certificate."
            )
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
    """How long to keep checking that a renewed certificate is actually served.

    Read from the provider that owns the action rather than from a file beside
    it. The bounds stay: this decides how long a renewal blocks, so a value the
    controller cannot live with is a failure here rather than an hour spent
    polling.
    """

    capability = controller_capability_registry().capabilities.get(CERTIFICATE_KIND)
    policy = capability.actions.get("renew") if capability else None
    verification = policy.verification if policy else None
    if verification is None:
        raise ProviderError("TLS renewal declares no verification policy.")
    timeout, interval = verification.timeout_seconds, verification.interval_seconds
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
                item["consumer"] for item in evidence if not item["matches_expected"]
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
    expected_fingerprint = _validate_certificate(
        fullchain, private_key, spec["domains"]
    )
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
        Path(_required("HQ", "ACME_DIR")) / "config" / "live" / spec["certificate_name"]
    )
    try:
        return lineage.joinpath("fullchain.pem").read_bytes(), lineage.joinpath(
            "privkey.pem"
        ).read_bytes()
    except OSError as exc:
        raise ProviderError(
            "Certbot lineage is unavailable for reconciliation."
        ) from exc


def apply_tls_reconcile(spec: dict[str, Any]) -> ProviderResult:
    fullchain, private_key = _lineage(spec)
    expected = _validate_certificate(fullchain, private_key, spec["domains"])
    observed = reconcile_tls(spec)
    fingerprints = {item["fingerprint_sha256"] for item in observed.status["consumers"]}
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
    caddy = next((item for item in spec["consumers"] if item["kind"] == "caddy"), None)
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
    caddy = next((item for item in spec["consumers"] if item["kind"] == "caddy"), None)
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


def _tls_reconcile(
    spec: dict[str, Any],
    *,
    apply: bool,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return apply_tls_reconcile(spec) if apply else reconcile_tls(spec)


def _tls_renew(
    spec: dict[str, Any],
    *,
    apply: bool,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    if apply:
        return renew_tls(spec)
    return ProviderResult(
        changed=True,
        status={},
        conditions=[],
        message="Certificate would be issued, deployed, verified, and rolled back on failure.",
    )


def reconcile_uploaded_certificate(
    spec: dict[str, Any], *, apply: bool = True
) -> ProviderResult:
    """Install a certificate HQ was given rather than one it issued.

    Deployment is identical -- a proxy does not care which authority signed the
    thing it serves -- so this reuses the same path as a renewal and differs
    only in where the material came from. It is never renewed here: the CA is
    air-gapped, and the certificate's expiry is reported so an operator knows
    when to generate the next one.
    """

    material = spec.get("material") or {}
    fullchain = material.get("fullchain") or ""
    private_key = material.get("private_key") or ""
    if not fullchain or not private_key:
        raise ProviderError(
            "HQ did not supply the stored certificate. Upload it again."
        )
    domains = list(material.get("domains") or ())
    if not apply:
        return ProviderResult(
            changed=True,
            status={"certificate_name": spec["certificate_name"], "domains": domains},
            conditions=[
                _condition("Ready", True, "Planned", "Would install the certificate.")
            ],
            message="Would install the stored certificate.",
        )
    deployment = _deploy_certificate(
        {
            "certificate_name": spec["certificate_name"],
            "domains": domains,
            "consumers": spec["consumers"],
        },
        fullchain.encode(),
        private_key.encode(),
    )
    observed = {
        key: value
        for key, value in deployment.items()
        # The deployment report carries an npm certificate identity; nothing
        # secret-bearing may enter HQ, and the status guard rejects the whole
        # report if it does.
        if "private" not in key and "key" not in key
    }
    return ProviderResult(
        changed=True,
        status={
            "certificate_name": spec["certificate_name"],
            "domains": domains,
            **observed,
        },
        conditions=[
            _condition("Ready", True, "Installed", "Stored certificate installed.")
        ],
        message="Stored certificate installed.",
    )


def delete_uploaded_certificate(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Remove an installed certificate, or refuse and say who has to do it.

    Only Nginx Proxy Manager can be undone from here. A Caddy target receives a
    certificate over an SSH forced command that implements ``deploy`` and
    nothing else, so removing one means editing the remote side -- and a delete
    that reported success while leaving a file on a host would take HQ's
    declaration with it and lose the only record that the file is there.

    Refused whole rather than done partly, for the same reason.
    """

    elsewhere = sorted(
        consumer["name"] for consumer in spec["consumers"] if consumer["kind"] != "npm"
    )
    if elsewhere:
        raise ProviderError(
            "HQ can only remove this from Nginx Proxy Manager. Take it off "
            + ", ".join(elsewhere)
            + " by hand first, then remove those targets from this resource."
        )

    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    certificates = _request(f"{base_url}/nginx/certificates", headers=headers)
    wanted = {f"Severino HQ - {consumer['name']}" for consumer in spec["consumers"]}
    matches = [item for item in certificates if item.get("nice_name") in wanted]
    if not matches:
        return ProviderResult(
            changed=False,
            status={"removed": True},
            conditions=[
                _condition("Ready", True, "Absent", "No such certificate in NPM.")
            ],
            message="Certificate was already absent from NPM.",
        )

    # A certificate still bound to a proxy host cannot be deleted without taking
    # TLS down on it. Naming the hosts is the actionable part: the operator has
    # to point them at something else first.
    hosts = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    identifiers = {item["id"] for item in matches}
    still_bound = sorted(
        name
        for host in hosts
        if host.get("certificate_id") in identifiers
        for name in host.get("domain_names", [])
    )
    if still_bound:
        raise ProviderError(
            "Still serving " + ", ".join(still_bound) + ". Point those at "
            "another certificate before removing this one."
        )
    if apply:
        for item in matches:
            _request(
                f"{base_url}/nginx/certificates/{item['id']}",
                method="DELETE",
                headers=headers,
            )
    return ProviderResult(
        changed=True,
        status={"removed": True},
        conditions=[
            _condition("Ready", True, "Removed", "Certificate removed from NPM.")
        ],
        message="Certificate removed from NPM.",
    )


def delete_npm(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Remove the proxy host matching this exact domain set."""

    base_url = _npm_url()
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


def list_npm() -> list[dict[str, Any]]:
    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    records = _request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    access_policies = _npm_access_policies(base_url, headers)
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
    # Which certificate each host actually serves. A name behind a proxy is
    # served over TLS or it is not, and until now HQ could only see the ones it
    # had been told about -- so an internal certificate, installed and working,
    # read as "no certificate covers this" on every name it served. The proxy
    # already knows; it just was not asked.
    served_by = _npm_certificates(base_url, headers)
    return [
        {
            **{field: record.get(field) for field in fields},
            "certificate": served_by.get(record.get("certificate_id"), {}),
            "access_policy": access_policies.get(record.get("access_list_id")),
        }
        for record in records
        if record.get("domain_names")
    ]


def _npm_access_policies(
    base_url: str, headers: dict[str, str]
) -> dict[Any, dict[str, Any]]:
    """Safe ingress rules, stripped of every authorization identity."""

    try:
        records = _request(
            f"{base_url}/nginx/access-lists?expand=items,clients", headers=headers
        )
    except (ProviderError, OSError, ValueError, KeyError):
        # A policy endpoint unavailable on an older NPM must not erase every
        # proxy host. The missing evidence stays unknown until the next sweep.
        return {}
    found = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), int):
            continue
        clients = [
            {
                "directive": str(client.get("directive", "")),
                "address": str(client.get("address", "")),
            }
            for client in record.get("clients") or ()
            if client.get("directive") and client.get("address")
        ]
        found[record["id"]] = {
            "name": str(record.get("name", "")),
            "satisfy_any": bool(record.get("satisfy_any", False)),
            "pass_auth": bool(record.get("pass_auth", False)),
            # Counts prove that NPM is not adding a second login without
            # carrying usernames, password hints, or any other auth material
            # into HQ's safe observation cache.
            "authorization_count": len(record.get("items") or ()),
            "clients": clients,
            # NPM generates a final ``deny all`` for every non-empty client
            # rule set but does not return that generated rule through the API.
            "implicit_deny": bool(clients),
        }
    return found


def _npm_certificates(
    base_url: str, headers: dict[str, str]
) -> dict[Any, dict[str, Any]]:
    """Every certificate the proxy holds, by the id a host refers to it with.

    Reported as an attribute of the host that serves it rather than as an
    inventory of its own. HQ does not hold the material for these -- the CA
    that signs them is deliberately air-gapped -- so a declaration it could
    never fulfil would be worse than the gap it closes. What it needs is to be
    able to see that the name is served over TLS, which is an observation.
    """

    try:
        certificates = _request(f"{base_url}/nginx/certificates", headers=headers)
    except (ProviderError, OSError, ValueError, KeyError):
        # One reading missing must not lose the proxy hosts themselves.
        return {}
    return {
        item.get("id"): {
            "name": str(item.get("nice_name", "")),
            "domains": [str(name) for name in item.get("domain_names") or ()],
            "expires_on": str(item.get("expires_on", "")),
            "provider": str(item.get("provider", "")),
        }
        for item in certificates or ()
        if item.get("id")
    }


# ----- Cloudflare ------------------------------------------------------------
#
# Two credentials, each scoped to one surface.
#
# `cloudflare_dns` is deliberately narrow: it can read the zones on the account
# and read and write their DNS records, and nothing else. Zone settings,
# analytics and every account-level surface answer 403 to it. That is why the
# zone provider declares no reconcile it could perform -- see the capability
# registry -- and why nothing here reaches for a setting it cannot change.
#
# `cloudflare_api` carries the account surface -- analytics, zone settings,
# registration -- and reaches no DNS record. Neither is a subset of the other,
# so a provider states which one it needs and gets exactly that.


def _cloudflare_url(
    connection_ref: str = "", *, provider: str = "cloudflare_dns"
) -> str:
    return _required(connection_prefix(provider, connection_ref), "URL").rstrip("/")


def _cloudflare_token(
    connection_ref: str = "", *, provider: str = "cloudflare_dns"
) -> str:
    return _required(connection_prefix(provider, connection_ref), "API_TOKEN")


def _cloudflare_envelope(
    path: str,
    *,
    method: str = "GET",
    payload: Any = None,
    provider: str = "cloudflare_dns",
    connection_ref: str = "",
) -> dict[str, Any]:
    """One Cloudflare call, returning the whole envelope with its errors kept.

    Not routed through ``_request`` because Cloudflare says something useful in
    the body of a 400 -- "Content for A record must be a valid IPv4 address",
    "An identical record already exists" -- and the shared helper turns every
    non-200 into the same sentence. A rejected DNS change that only says
    "Provider request failed: HTTPError" is a support ticket to yourself.

    ``success`` is checked here rather than by each caller, because Cloudflare
    also answers 200 with ``success: false`` -- a token missing one permission
    returns no ``result`` at all, and a list helper reading ``result`` off that
    collects nothing and reports an empty estate. An account that looks empty
    and an account that refused to answer must not read the same.

    Both credentials come through here: the zone-scoped DNS token and the
    account-scoped analytics one differ only in which connection names them,
    which is what ``provider`` and ``connection_ref`` select.
    """

    url = f"{_cloudflare_url(connection_ref, provider=provider)}{path}"
    headers = {
        "Authorization": (
            f"Bearer {_cloudflare_token(connection_ref, provider=provider)}"
        ),
        "Accept": "application/json",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is deployment config.
            request, timeout=15, context=_tls_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            f"Cloudflare refused the request: {_cloudflare_errors(exc.read())}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(
            f"Cloudflare request failed: {type(exc).__name__}."
        ) from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ProviderError("Cloudflare returned invalid JSON.") from exc
    if not parsed.get("success", False):
        raise ProviderError(
            f"Cloudflare refused the request: {_cloudflare_errors(raw)}"
        )
    return parsed if isinstance(parsed, dict) else {}


def _cloudflare_request(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    """The zone-scoped DNS surface, unwrapped to the result callers expect."""

    return _cloudflare_envelope(path, method=method, payload=payload).get("result")


def _cloudflare_errors(raw: bytes) -> str:
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return "an unreadable error"
    messages = [
        str(error.get("message", "")).strip()
        for error in parsed.get("errors") or ()
        if str(error.get("message", "")).strip()
    ]
    return "; ".join(messages) or "no reason given"


def _cloudflare_paged(path: str) -> list[dict[str, Any]]:
    """Every page of a list endpoint.

    Cloudflare returns 100 records at most. A zone that outgrew one page would
    otherwise have its tail silently reported as absent -- and "absent" is the
    word this system acts on, so the reconciler would set about recreating
    records that were there all along.
    """

    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        result = _cloudflare_request(f"{path}{separator}per_page=100&page={page}")
        batch = result or []
        collected.extend(batch)
        if len(batch) < 100:
            return collected
        page += 1
        if page > 50:
            raise ProviderError("Cloudflare list did not terminate.")


def _cloudflare_zones() -> list[dict[str, Any]]:
    return _snapshot_value(("cloudflare-zones",), lambda: _cloudflare_paged("/zones"))


_ZONE_IDS: dict[str, str] = {}


def _cloudflare_zone_id(zone: str) -> str:
    wanted = zone.strip().lower().rstrip(".")
    if wanted in _ZONE_IDS:
        return _ZONE_IDS[wanted]
    for candidate in _cloudflare_zones():
        name = str(candidate.get("name", "")).strip().lower()
        if name:
            _ZONE_IDS[name] = candidate["id"]
    if wanted not in _ZONE_IDS:
        raise ProviderError(
            f"The Cloudflare credential cannot see a zone called {wanted!r}."
        )
    return _ZONE_IDS[wanted]


def _cloudflare_records(zone_id: str) -> list[dict[str, Any]]:
    return _cloudflare_paged(f"/zones/{zone_id}/dns_records")


def _caa_data(content: str) -> dict[str, Any]:
    """A CAA value in the three-field shape Cloudflare will accept.

    Split by the same parser the spec validates with, so a value the form
    accepted cannot be one this refuses.
    """

    parts = caa_parts(content)
    if parts is None:
        raise ProviderError('A CAA value must look like: 0 issue "letsencrypt.org".')
    flags, tag, value = parts
    return {"flags": flags, "tag": tag, "value": value}


def _cloudflare_payload(spec: dict[str, Any]) -> dict[str, Any]:
    record_type = str(spec["record_type"]).upper()
    payload: dict[str, Any] = {
        "type": record_type,
        "name": normalized_hostname(spec["name"]),
        "ttl": int(spec.get("ttl", 1) or 1),
    }
    if record_type == "CAA":
        payload["data"] = _caa_data(str(spec["content"]))
    else:
        payload["content"] = normalized_record_content(
            record_type, str(spec["content"])
        )
    if record_type == "MX":
        payload["priority"] = int(spec.get("priority") or 0)
    if record_type in {"A", "AAAA", "CNAME"}:
        # Sent only for the types that can carry it. Cloudflare rejects the
        # field outright on a TXT or MX record rather than ignoring it.
        payload["proxied"] = bool(spec.get("proxied", False))
    return payload


def _record_matches(live: dict[str, Any], spec: dict[str, Any]) -> bool:
    record_type = str(spec["record_type"]).upper()
    if str(live.get("type", "")).upper() != record_type:
        return False
    if normalized_hostname(live.get("name", "")) != normalized_hostname(spec["name"]):
        return False
    return normalized_record_content(
        record_type, str(live.get("content", ""))
    ) == normalized_record_content(record_type, str(spec["content"]))


def _record_status(zone: str, live: dict[str, Any]) -> dict[str, Any]:
    return {
        "zone": zone,
        # Carried so the next reconciliation can find this exact record even if
        # its name, type or value were all edited at once. Without it, an edit
        # that changes the value looks like a brand new record and the old one
        # is left behind, answering, with nothing in HQ pointing at it.
        "record_id": live.get("id", ""),
        "name": live.get("name", ""),
        "record_type": str(live.get("type", "")).upper(),
        "content": live.get("content", ""),
        "priority": live.get("priority"),
        "proxied": bool(live.get("proxied", False)),
        "ttl": live.get("ttl", 1),
    }


def reconcile_cloudflare_record(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Make one public DNS record match its declaration.

    Identity is the recorded Cloudflare record id where there is one, and the
    name/type/value triple otherwise. That order matters: a zone apex commonly
    holds several records of one type, so matching by name alone would edit
    whichever of four CAA records happened to come back first.
    """

    zone = str(spec["zone"]).strip().lower().rstrip(".")
    zone_id = _cloudflare_zone_id(zone)
    records = _cloudflare_records(zone_id)
    desired = _cloudflare_payload(spec)

    record_id = str((observed or {}).get("record_id", "")).strip()
    live = next((item for item in records if item.get("id") == record_id), None)
    if live is None:
        live = next((item for item in records if _record_matches(item, spec)), None)

    if live is None:
        if apply:
            live = _cloudflare_request(
                f"/zones/{zone_id}/dns_records", method="POST", payload=desired
            )
        return ProviderResult(
            changed=True,
            status=_record_status(zone, live or {}),
            conditions=[
                _condition("Ready", True, "Created", "DNS record was created.")
            ],
            message="Public DNS record created.",
        )

    current = {
        "type": str(live.get("type", "")).upper(),
        "name": normalized_hostname(live.get("name", "")),
        "ttl": int(live.get("ttl", 1) or 1),
    }
    if desired["type"] == "CAA":
        current["data"] = {
            key: (live.get("data") or {}).get(key) for key in ("flags", "tag", "value")
        }
    else:
        current["content"] = normalized_record_content(
            desired["type"], str(live.get("content", ""))
        )
    if desired["type"] == "MX":
        current["priority"] = int(live.get("priority") or 0)
    if "proxied" in desired:
        current["proxied"] = bool(live.get("proxied", False))

    if current == desired:
        return ProviderResult(
            changed=False,
            status=_record_status(zone, live),
            conditions=[
                _condition("Ready", True, "Reconciled", "DNS record is current.")
            ],
            message="Public DNS record unchanged.",
        )
    if apply:
        live = _cloudflare_request(
            f"/zones/{zone_id}/dns_records/{live['id']}",
            method="PUT",
            payload=desired,
        )
    return ProviderResult(
        changed=True,
        status=_record_status(zone, live or {}),
        conditions=[_condition("Ready", True, "Reconciled", "DNS record was updated.")],
        message="Public DNS record updated.",
    )


def delete_cloudflare_record(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Remove one record, treating an already-absent one as success.

    Only ever the single record this declaration owns. Cloudflare deletes by id,
    which is the one safe way to do this: a zone apex may hold nine records, and
    a delete that matched on name would take the other eight with it.
    """

    zone = str(spec["zone"]).strip().lower().rstrip(".")
    zone_id = _cloudflare_zone_id(zone)
    records = _cloudflare_records(zone_id)

    record_id = str((observed or {}).get("record_id", "")).strip()
    live = next((item for item in records if item.get("id") == record_id), None)
    if live is None:
        live = next((item for item in records if _record_matches(item, spec)), None)
    if live is None:
        return ProviderResult(
            changed=False,
            status={"zone": zone, "name": spec.get("name", ""), "removed": True},
            conditions=[
                _condition("Ready", True, "Absent", "No such record in Cloudflare.")
            ],
            message="Public DNS record was already absent.",
        )
    if apply:
        _cloudflare_request(
            f"/zones/{zone_id}/dns_records/{live['id']}", method="DELETE"
        )
    return ProviderResult(
        changed=True,
        status={"zone": zone, "name": spec.get("name", ""), "removed": True},
        conditions=[_condition("Ready", True, "Removed", "DNS record was removed.")],
        message="Public DNS record removed.",
    )


# The zone settings worth carrying: how a domain answers over TLS. Named rather
# than taken whole, because the settings endpoint returns eighty entries and
# most of them -- minify, rocket loader, browser cache TTL -- are not posture.
ZONE_POSTURE_SETTINGS = (
    "ssl",
    "min_tls_version",
    "tls_1_3",
    "always_use_https",
    "automatic_https_rewrites",
)


def _registrar_domains() -> dict[str, dict[str, Any]]:
    """What the registrar holds for every domain on the account, by name.

    Read from Cloudflare rather than from RDAP, which was the first attempt.
    RDAP is public and needs no credential, and it can only ever answer *when* a
    domain expires. The registrar knows whether it will renew itself -- and that
    is the fact worth acting on. A domain three months out with auto-renew on is
    a date; the same domain with auto-renew off is an outage with a countdown,
    and nothing else in HQ would know the difference.

    The account credential already carries the registration surface, so this
    costs no new secret. A refusal leaves the map empty and the zones report
    their records as before.
    """

    try:
        account = _analytics_account()
        domains = _cloudflare_api_list(f"/accounts/{account}/registrar/domains")
    except (ProviderError, OSError, ValueError):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for domain in domains:
        name = str(domain.get("name", "")).strip().lower().rstrip(".")
        if not name:
            continue
        found[name] = {
            "expires_at": str(domain.get("expires_at", ""))[:10],
            "auto_renew": bool(domain.get("auto_renew")),
            "locked": bool(domain.get("locked")),
            "registrar": "Cloudflare",
        }
    return found


def _cloudflare_zone_posture(zone_id: str) -> dict[str, str]:
    """How a zone answers over TLS, read through the credential that can see it.

    The DNS token cannot: it holds records and nothing else, which is why this
    was blank for as long as it existed. `cloudflare_api` carries the account
    surface, zone settings included, and has been sitting beside it.

    A failure here is not a failed sweep. The account token is a separate
    credential with its own permissions, and a zone that answers with its
    records and no posture is a smaller thing to report than no zone at all --
    so a refusal leaves the key absent and the page says nothing rather than
    guessing.
    """

    if not zone_id:
        return {}
    try:
        envelope = _cloudflare_api_request(f"/zones/{zone_id}/settings")
    except (ProviderError, OSError, ValueError):
        return {}
    settings = (envelope or {}).get("result")
    if not isinstance(settings, list):
        return {}
    return {
        str(item["id"]): str(item.get("value", ""))
        for item in settings
        if isinstance(item, dict)
        and item.get("id") in ZONE_POSTURE_SETTINGS
        and item.get("value") not in (None, "")
    }


def list_cloudflare_zones() -> list[dict[str, Any]]:
    """Every zone the credential can see, declared or not.

    Reported in full deliberately: which of them HQ should manage is an
    operator's decision, and it cannot be made on a screen that only lists the
    ones already decided about.
    """

    connection_ref = _required(connection_prefix("cloudflare_dns"), "CONNECTION_REF")
    # Read once for the whole sweep rather than once per zone: it is one list
    # for the account, and asking per zone would be four calls for one answer.
    registrars = _registrar_domains()
    return [
        {
            "zone": zone["name"],
            "connection_ref": connection_ref,
            "status": zone.get("status", ""),
            "plan": (zone.get("plan") or {}).get("name", ""),
            "posture": _cloudflare_zone_posture(str(zone.get("id", ""))),
            "registration": registrars.get(
                str(zone.get("name", "")).strip().lower().rstrip("."), {}
            ),
        }
        for zone in _cloudflare_zones()
        if zone.get("name")
    ]


def list_cloudflare_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for zone in _cloudflare_zones():
        zone_name = zone.get("name")
        if not zone_name:
            continue
        for live in _cloudflare_records(zone["id"]):
            if not live.get("type") or not live.get("name"):
                continue
            records.append(_record_status(zone_name, live))
    return records


# --- Portainer ---------------------------------------------------------------
#
# Portainer holds one credential and reaches every Docker host registered with
# it, so a machine becomes available to HQ by being an environment there rather
# than by anything here naming it.


def _portainer_url(connection_ref: str = "") -> str:
    base = _required(connection_prefix("portainer", connection_ref), "URL").rstrip("/")
    return base if base.endswith("/api") else f"{base}/api"


def _portainer_headers(connection_ref: str = "") -> dict[str, str]:
    return {
        "X-API-Key": _required(
            connection_prefix("portainer", connection_ref), "API_TOKEN"
        )
    }


def _an_address(host: str) -> str:
    """A name resolved to the address it answers at, where that is possible.

    A credential is written the way an operator types it, which is a hostname.
    An address is what every other source of a machine reports, so a hostname
    left unresolved joins to nothing: HQ holds one machine's address from three
    directions and a name for it from a fourth, and cannot see they are the
    same machine. Resolving is what makes the fourth comparable to the rest.

    The name is kept when it does not resolve. That is not a failure worth
    raising -- an unresolvable name still identifies the endpoint consistently,
    which is most of what this is for.
    """

    if not host or _looks_like_an_address(host):
        return host
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return host


def _looks_like_an_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _load_portainer_environments(connection_ref: str = "") -> list[dict[str, Any]]:
    """Every Docker environment, with the machine each one is.

    Portainer names its own local environment `local`, which is nobody's
    hostname. The address it is reached at is the reliable identity: an agent
    carries the machine's address in its URL, and a unix socket means the
    machine Portainer is itself running on.
    """

    environments = _request(
        f"{_portainer_url(connection_ref)}/endpoints",
        headers=_portainer_headers(connection_ref),
    )
    # Where Portainer itself is, which is where its local socket is too. Without
    # this a local environment reports no address at all, and an address is the
    # one identity every source of a machine agrees on -- so the machine
    # Portainer runs on was the single one HQ could not recognise by it.
    portainer_at = _an_address(
        urllib.parse.urlsplit(_portainer_url(connection_ref)).hostname or ""
    )
    resolved = []
    for environment in environments or []:
        url = str(environment.get("URL", ""))
        address = urllib.parse.urlsplit(url).hostname if "://" in url else ""
        resolved.append(
            {
                "id": environment.get("Id"),
                "name": environment.get("Name", ""),
                "address": address or portainer_at,
                "local": not address,
                "reachable": environment.get("Status") == 1,
            }
        )
    return resolved


def _portainer_environments(connection_ref: str = "") -> list[dict[str, Any]]:
    return _snapshot_value(
        ("portainer-environments", connection_ref),
        lambda: _load_portainer_environments(connection_ref),
    )


def _portainer_environment_for(host: str, connection_ref: str = "") -> dict[str, Any]:
    """The environment that is a given machine.

    Matched on address, or on the environment's own name, or -- for the local
    socket -- on the machine Portainer runs on, which the controller knows
    because it is the machine it runs on too.
    """

    environments = _portainer_environments(connection_ref)
    for environment in environments:
        if environment["address"] and environment["address"] == host:
            return environment
        if environment["name"] == host:
            return environment
    local_host = controller_id()
    for environment in environments:
        if environment["local"] and local_host and local_host == host:
            return environment
    raise ProviderError(f"No Portainer environment is {host!r}.")


def _portainer_stacks(
    environment_id: int, connection_ref: str = ""
) -> list[dict[str, Any]]:
    stacks = _request(
        f"{_portainer_url(connection_ref)}/stacks",
        headers=_portainer_headers(connection_ref),
    )
    return [
        stack for stack in stacks or [] if stack.get("EndpointId") == environment_id
    ]


def _portainer_containers(
    environment_id: int, connection_ref: str = ""
) -> list[dict[str, Any]]:
    return (
        _request(
            f"{_portainer_url(connection_ref)}/endpoints/{environment_id}"
            "/docker/containers/json?all=1",
            headers=_portainer_headers(connection_ref),
        )
        or []
    )


def _published(container: dict[str, Any]) -> tuple[list[int], int | None, bool]:
    """Every port this answers on, the one that is unambiguous, and its reach.

    A published port carries the address it was bound to. Bound to the loopback
    it is reachable only from inside that machine, which is the difference
    between a proxy in a container reaching it and returning 502.

    The single port is named only when exactly one is published. A proxy
    publishing 80, 81 and 443 has no one port, and picking the first would print
    a guess beside facts.
    """

    ports: set[int] = set()
    reachable = True
    for port in container.get("Ports") or ():
        public = port.get("PublicPort")
        if not public:
            continue
        ports.add(int(public))
        if str(port.get("IP", "")) in {"127.0.0.1", "::1"}:
            reachable = False
    listed = sorted(ports)
    return listed, (listed[0] if len(listed) == 1 else None), reachable


def _container_record(
    container: dict[str, Any],
    host: str,
    connection_ref: str,
    portainer_stacks: frozenset[str] = frozenset(),
    host_address: str = "",
) -> dict[str, Any]:
    """What Portainer knows about one container, in HQ's vocabulary."""

    labels = container.get("Labels") or {}
    ports, port, reachable = _published(container)
    stack = labels.get("com.docker.compose.project", "")
    return {
        # Whether Portainer created this, or merely sees it. Everything running
        # today was started by compose on the machine, so Portainer holds no
        # stack for any of it -- and a declaration built as though it did would
        # ask Portainer to stand up a second copy of something already serving.
        "portainer_managed": bool(stack) and stack in portainer_stacks,
        "name": (container.get("Names") or ["/"])[0].lstrip("/"),
        "stack": stack,
        "working_dir": labels.get("com.docker.compose.project.working_dir", ""),
        "image": container.get("Image", ""),
        # How it is attached, because it decides whether the ports below can
        # mean anything. A container on the host network binds the machine's
        # ports directly and Docker reports none for it, so an empty list is
        # "cannot be known from here" rather than "publishes nothing" -- and
        # only this field tells the two apart.
        "network_mode": (container.get("HostConfig") or {}).get("NetworkMode", ""),
        "state": container.get("State", ""),
        "status": container.get("Status", ""),
        "ports": ports,
        "port": port,
        "reachable": reachable,
        "host": host,
        # Where the machine is, not just what this credential calls it. Two
        # credentials name one machine differently -- an SSH item and a
        # Portainer environment for the same VPS -- and the address is the only
        # thing both agree on. Without it HQ lists one machine twice and files
        # its containers under whichever name the sweep used.
        "host_address": host_address,
        "connection_ref": connection_ref,
    }


def _stack_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "Name": spec["name"],
        "StackFileContent": spec["compose"],
        "Env": [
            {"name": item.get("name", ""), "value": item.get("value", "")}
            for item in spec.get("environment") or ()
        ],
    }


def reconcile_portainer(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    connection_ref = spec.get("connection_ref", "")
    environment = _portainer_environment_for(spec["host"], connection_ref)
    if not environment["reachable"]:
        raise ProviderError(f"Portainer cannot currently reach {spec['host']}.")
    existing = [
        stack
        for stack in _portainer_stacks(environment["id"], connection_ref)
        if stack.get("Name") == spec["name"]
    ]
    if len(existing) > 1:
        raise ProviderError("Portainer holds more than one stack of that name.")

    changed = True
    if existing:
        stack = existing[0]
        current = _request(
            f"{_portainer_url(connection_ref)}/stacks/{stack['Id']}/file",
            headers=_portainer_headers(connection_ref),
        )
        if (current or {}).get("StackFileContent") == spec["compose"]:
            changed = False
        elif apply:
            _request(
                f"{_portainer_url(connection_ref)}/stacks/{stack['Id']}"
                f"?endpointId={environment['id']}",
                method="PUT",
                headers=_portainer_headers(connection_ref),
                payload={**_stack_payload(spec), "PullImage": False},
            )
    elif apply:
        _request(
            f"{_portainer_url(connection_ref)}/stacks/create/standalone/string"
            f"?endpointId={environment['id']}",
            method="POST",
            headers=_portainer_headers(connection_ref),
            payload=_stack_payload(spec),
        )

    # What is actually running, which is the only thing worth reporting: a
    # stack Portainer accepted and Docker then failed to start is not Ready.
    containers = [
        _container_record(container, spec["host"], connection_ref)
        for container in _portainer_containers(environment["id"], connection_ref)
        if (container.get("Labels") or {}).get("com.docker.compose.project")
        == spec["name"]
    ]
    running = [item for item in containers if item["state"] == "running"]
    unreachable = [item for item in containers if not item["reachable"]]
    status = {
        "environment": environment["name"],
        "host": spec["host"],
        "containers": containers,
        "origin": f"{spec['host']}:{spec['port']}" if spec.get("port") else "",
        "state": "running" if running and len(running) == len(containers) else "",
    }

    if apply and not containers:
        return ProviderResult(
            changed=changed,
            status=status,
            conditions=[
                _condition(
                    "Degraded",
                    True,
                    "NotRunning",
                    "The stack exists in Portainer but no container from it is "
                    "running.",
                )
            ],
            message="Stack is declared but nothing is running.",
        )
    if unreachable:
        names = ", ".join(sorted(item["name"] for item in unreachable))
        return ProviderResult(
            changed=changed,
            status=status,
            conditions=[
                _condition(
                    "Degraded",
                    True,
                    "BoundToLoopback",
                    f"{names} publishes a port on the loopback address, so "
                    "nothing outside that machine can reach it -- including a "
                    "proxy running in a container on the same host.",
                )
            ],
            message="Stack is running but is not reachable.",
        )
    return ProviderResult(
        changed=changed,
        status=status,
        conditions=[_condition("Ready", True, "Reconciled", "Stack is running.")],
        message="Stack updated." if changed else "Stack unchanged.",
    )


def delete_portainer(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    connection_ref = spec.get("connection_ref", "")
    environment = _portainer_environment_for(spec["host"], connection_ref)
    existing = [
        stack
        for stack in _portainer_stacks(environment["id"], connection_ref)
        if stack.get("Name") == spec["name"]
    ]
    if not existing:
        return ProviderResult(
            changed=False,
            status={},
            conditions=[_condition("Ready", True, "Absent", "Stack is already gone.")],
            message="Stack was already absent.",
        )
    if apply:
        _request(
            f"{_portainer_url(connection_ref)}/stacks/{existing[0]['Id']}"
            f"?endpointId={environment['id']}",
            method="DELETE",
            headers=_portainer_headers(connection_ref),
        )
    return ProviderResult(
        changed=True,
        status={},
        conditions=[_condition("Ready", True, "Deleted", "Stack removed.")],
        message="Stack removed.",
    )


def _list_portainer_containers() -> list[dict[str, Any]]:
    """Every container Portainer can see, on every machine it reaches.

    Containers rather than stacks, and reported as such. A stack listing
    describes only what Portainer itself created, and everything standing up
    today was started by compose on the machine -- so a stack listing reports an
    estate of nothing while ten containers run.

    The distinction is not pedantic. Docker will start, stop and restart any
    container; Portainer will only do so for a stack it made. Modelling what is
    running as a container is what lets HQ cycle one it did not create.
    """

    local_host = controller_id()
    records: list[dict[str, Any]] = []
    for connection_ref in provider_connection_refs("portainer"):
        for environment in _portainer_environments(connection_ref):
            if not environment["reachable"]:
                continue
            # A local socket is the machine the controller runs on, which is the
            # name the topology and every other resource already use for it.
            host = (
                local_host
                if environment["local"] and local_host
                else environment["name"]
            )
            created_here = frozenset(
                str(stack.get("Name", ""))
                for stack in _portainer_stacks(environment["id"], connection_ref)
                if stack.get("Name")
            )
            for container in _portainer_containers(environment["id"], connection_ref):
                # The controller is running this sweep from inside one of these.
                # Reporting it adds a row that is gone before the page renders.
                if (container.get("Labels") or {}).get(
                    "severino-hq.role"
                ) == "controller":
                    continue
                records.append(
                    _container_record(
                        container,
                        host,
                        connection_ref,
                        created_here,
                        environment["address"],
                    )
                )
    return records


def list_portainer_containers() -> list[dict[str, Any]]:
    return _snapshot_value(("portainer-containers",), _list_portainer_containers)


def _portainer_container_id(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The Docker id of a declared container, and the environment holding it.

    Looked up by name on each pass rather than stored. A container id changes
    every time it is recreated, and a stored one would address something that no
    longer exists -- silently, because Docker answers "no such container" the
    same way whether it never existed or was replaced this morning.
    """

    connection_ref = spec.get("connection_ref", "")
    environment = _portainer_environment_for(spec["host"], connection_ref)
    wanted = spec["name"]
    for container in _portainer_containers(environment["id"], connection_ref):
        names = [str(name).lstrip("/") for name in container.get("Names") or ()]
        if wanted in names:
            return str(container.get("Id", "")), environment
    raise ProviderError(f"No container named {wanted!r} on {spec['host']}.")


def _cycle_portainer_container(
    spec: dict[str, Any], verb: str, *, apply: bool
) -> ProviderResult:
    """Start, stop or restart one container, and report what it did.

    Docker answers these with 204 on success and 304 when the container is
    already in the state asked for, so "start an already-running container" is
    not an error and must not be reported as one.
    """

    connection_ref = spec.get("connection_ref", "")
    container_id, environment = _portainer_container_id(spec)
    if apply:
        _request(
            f"{_portainer_url(connection_ref)}/endpoints/{environment['id']}"
            f"/docker/containers/{container_id}/{verb}",
            method="POST",
            headers=_portainer_headers(connection_ref),
            payload={},
        )
    # Read back rather than trusting the call. A restart that brought the
    # container up and let it exit two seconds later reports success at the API
    # and is not what was asked for.
    observed = [
        _container_record(container, spec["host"], connection_ref)
        for container in _portainer_containers(environment["id"], connection_ref)
        if spec["name"]
        in [str(name).lstrip("/") for name in container.get("Names") or ()]
    ]
    state = observed[0]["state"] if observed else ""
    status = {
        "host": spec["host"],
        "container": spec["name"],
        "state": state,
        "containers": observed,
    }
    settled = state == ("exited" if verb == "stop" else "running")
    return ProviderResult(
        changed=apply,
        status=status,
        conditions=[
            _condition(
                "Ready" if settled else "Degraded",
                True,
                verb.capitalize() + ("ed" if verb == "stop" else "ed"),
                f"{spec['name']} is {state or 'in an unknown state'}.",
            )
        ],
        message=f"{spec['name']} is {state or 'in an unknown state'}.",
    )


def restart_portainer_container(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return _cycle_portainer_container(spec, "restart", apply=apply)


def start_portainer_container(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return _cycle_portainer_container(spec, "start", apply=apply)


def stop_portainer_container(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return _cycle_portainer_container(spec, "stop", apply=apply)


TAILNET_STATUS = os.environ.get("SEVERINO_TAILNET_STATUS", "")
# Tailnet lock, handed over the same way and for the same reason: the local
# API is read *and* write, so the controller is given a reading rather than the
# socket. Separately optional -- a tailnet without lock enabled answers it
# perfectly well, and a daemon too old to know it should cost the sweep
# nothing.
TAILNET_LOCK = os.environ.get("SEVERINO_TAILNET_LOCK", "")


def _tailnet_lock() -> dict[str, Any]:
    """Whether tailnet lock is on, and who it is currently shutting out.

    The fact with the least warning attached. Under lock a node whose key no
    signing node has signed is not degraded, it is *absent*: every other node
    filters it out, and the node itself reports being perfectly healthy. There
    is nothing in a status page or a service check that says why.
    """

    if not TAILNET_LOCK:
        return {}
    try:
        status = json.loads(Path(TAILNET_LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    return {
        "enabled": bool(status.get("Enabled")),
        # Whether the machine taking this reading is itself signed. A "no" here
        # is why the rest of the tailnet cannot see it.
        "node_key_signed": bool(status.get("NodeKeySigned")),
        "trusted_keys": len(status.get("TrustedKeys") or ()),
        # Named, not counted: a locked-out node is a machine somebody has to go
        # and sign, and a number does not say which.
        "locked_out": sorted(
            str(peer.get("Name") or peer.get("StableID") or "")
            for peer in status.get("FilteredPeers") or ()
        ),
    }


def local_tailnet_devices() -> list[dict[str, Any]]:
    """The tailnet as the local daemon sees it, and nothing more.

    Kept apart from the enriched sweep because the two have different costs and
    different reasons. This one needs no credential and no network call beyond
    a unix socket, so anything that only wants to know what a device *is* --
    reconciling one, for instance -- asks this rather than paying for a policy
    read it will not use.
    """

    if not TAILNET_STATUS:
        raise ProviderError(
            "This controller was not given a tailnet reading, so it cannot say "
            "which machines are up."
        )
    try:
        raw = Path(TAILNET_STATUS).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProviderError(
            "The tailnet reading is missing. It is taken from the local "
            "daemon before this container starts, and only when there is one."
        ) from exc
    try:
        status = json.loads(raw)
    except ValueError as exc:
        raise ProviderError("The tailnet reading is not readable status.") from exc
    nodes = [status.get("Self") or {}, *(status.get("Peer") or {}).values()]
    found = [record for record in map(_tailnet_record, nodes) if record]
    # Which of them took the reading. Every other device is described from its
    # point of view -- the relay carrying it, the bytes exchanged with it -- so
    # a reader that cannot tell which one is the observer is reading a set of
    # measurements with no origin.
    for record in found[:1]:
        record["self"] = True
    return found


def list_tailnet_devices() -> list[dict[str, Any]]:
    """Every machine on the tailnet, with what the policy says about each.

    Read from the daemon this machine is already a peer of rather than from
    Tailscale's API. There is no credential to hold, render or rotate: a node's
    view of its own tailnet is something it has by being on it, and a controller
    that cannot be given a token cannot leak one.

    Handed in as a file rather than fetched here. The daemon's local API is read
    *and* write, with no read-only mode, so a controller holding its socket
    could log this machine off the tailnet -- and this is the process that holds
    every provider credential. It only ever needed the reading, so the reading
    is what it gets.

    It answers the question no other sweep can. Every other provider reports
    whether a *service* answered, so a machine whose Portainer token expired and
    a machine that is switched off are indistinguishable. This tells them apart.

    The local view is deliberately not the whole picture -- tags, the policy
    file and the tailnet's DNS configuration are control-plane facts the daemon
    does not hold. What it does hold is presence and key expiry, which are the
    two that go wrong quietly.
    """

    devices = local_tailnet_devices()
    # Who may reach each one, where a credential makes that answerable. Folded
    # into the device rather than swept separately: it is a fact about that
    # device, and a second inventory kind would be a second thing to join.
    reach = _reach_by_device(devices)
    try:
        identities = _tailnet_identities(_tailnet_token(""))
    except ProviderError:
        identities = {}
    for device in devices:
        device["reach"] = reach.get(device["name"], [])
        identity = identities.get(device["name"], {})
        device["user"] = identity.get("user", "")
        device["tags"] = identity.get("tags", [])
        device["advertised_routes"] = identity.get("advertised_routes", [])
        device["enabled_routes"] = identity.get("enabled_routes", [])
        device["authorized"] = bool(identity.get("authorized", True))
        device["lock_error"] = identity.get("lock_error", "")
        device["update_available"] = bool(identity.get("update_available"))
        device["client_version"] = identity.get("client_version", "")
        device["ssh_enabled"] = bool(identity.get("ssh_enabled"))
        device["blocks_incoming"] = bool(identity.get("blocks_incoming"))
        device["external"] = bool(identity.get("external"))
        # The coordination server's answer wins over the daemon's inference:
        # the daemon reports no expiry date, which is the same shape whether
        # expiry is disabled or the reading simply lacks it.
        if "key_expiry_disabled" in identity:
            device["key_expiry_disabled"] = bool(identity["key_expiry_disabled"])
        # The daemon's `ExitNodeOption` answers "can this node be my exit node",
        # which is already false for one that offers but was never approved.
        # The coordination server is the only side that can tell those apart,
        # so when it answered, its answer wins.
        if device["name"] in identities:
            device["offers_exit_node"] = bool(identity.get("offers_exit_node"))
            device["exit_node_approved"] = bool(identity.get("exit_node_approved"))
        else:
            device["exit_node_approved"] = device.get("offers_exit_node", False)
    return devices


def _tailnet_record(node: dict[str, Any]) -> dict[str, Any] | None:
    """One machine, keyed by the name the rest of HQ already calls it.

    Every field is optional. Tailscale omits rather than nulls -- a device with
    expiry disabled has no ``KeyExpiry`` at all, and a machine that has never
    been seen has no ``LastSeen`` -- so a reader that requires any of them
    rejects exactly the devices it exists to describe.
    """

    name = str(node.get("HostName") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        # The node's WireGuard public key. The cryptographic identity itself:
        # a peering is not a claim in an inventory, it is two keys that have
        # completed a handshake, and this is the half that can be shown.
        "public_key": str(node.get("PublicKey") or ""),
        # The MagicDNS name, which is how the tailnet addresses it and not
        # always what the host calls itself.
        "dns_name": str(node.get("DNSName") or "").rstrip("."),
        "online": bool(node.get("Online")),
        "last_seen": str(node.get("LastSeen") or ""),
        # Absent means expiry is disabled for this device, which is a different
        # statement from "expires at some unknown time" and is kept distinct.
        "key_expires": str(node.get("KeyExpiry") or ""),
        "addresses": [str(address) for address in node.get("TailscaleIPs") or ()],
        "os": str(node.get("OS") or ""),
        # Two different questions, and only the second is a fact about the
        # device. `ExitNode` is whether this peer is the exit node *this*
        # machine is currently routing through -- a statement about our own
        # preference. `ExitNodeOption` is whether the peer offers to be one at
        # all. A machine page saying "exit node" means the latter.
        "exit_node_in_use": bool(node.get("ExitNode")),
        "offers_exit_node": bool(node.get("ExitNodeOption")),
        "self": False,
        # How the traffic actually gets there, which is the part nothing else
        # can answer. A peer is either reached directly -- the two daemons found
        # a path through both NATs -- or carried by a relay, and the difference
        # is a real one an operator otherwise has to shell in to see. Absent
        # means the peer is idle rather than unreachable: a path is negotiated
        # when there is traffic, so a machine nobody is talking to has neither.
        "direct_endpoint": str(node.get("CurAddr") or ""),
        # Every address this node can be reached at off the tailnet -- the one
        # its router hands out and the one the internet sees it as. Reported
        # only for the node taking the reading; a peer's own list is not
        # something the daemon is told.
        "endpoints": [str(endpoint) for endpoint in node.get("Addrs") or ()],
        "relay": str(node.get("Relay") or ""),
        "last_handshake": str(node.get("LastHandshake") or ""),
        "active": bool(node.get("Active")),
        "rx_bytes": int(node.get("RxBytes") or 0),
        "tx_bytes": int(node.get("TxBytes") or 0),
    }


TAILNET_API = "https://api.tailscale.com/api/v2"


def _tailnet_token(connection_ref: str) -> str:
    """Exchange once per sweep and retain no token beyond that snapshot.

    The token lasts an hour, but a process-wide cache adds expiry and revocation
    behavior to get wrong. One sweep needs it several times, so that sweep shares
    one exchange and drops the result when its snapshot closes. The client itself
    remains held by the vault rather than by this process.
    """

    prefix = connection_prefix("tailscale", connection_ref)

    def exchange() -> str:
        client_id = _required(prefix, "CLIENT_ID")
        client_secret = _required(prefix, "CLIENT_SECRET")
        body = urllib.parse.urlencode(
            {"client_id": client_id, "client_secret": client_secret}
        ).encode()
        request = urllib.request.Request(
            f"{TAILNET_API}/oauth/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError("OAuth response is not an object")
                token = payload.get("access_token", "")
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"Tailscale refused the credential for {connection_ref} "
                f"({exc.code}). It has to be an OAuth client, not an API key."
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError("Tailscale did not answer the token request.") from exc
        if not token:
            raise ProviderError("Tailscale returned no access token.")
        return token

    return _snapshot_value(("tailscale-token", prefix), exchange)


def _tailnet_device_id(name: str) -> str:
    """The device's stable id, taken from the local daemon rather than the API.

    The reading this controller already has carries it, so finding which device
    to change costs no call and no credential. The token is spent on the change
    itself and on nothing else.
    """

    for node in _tailnet_nodes():
        if str(node.get("HostName") or "").strip() == name:
            identifier = str(node.get("ID") or "")
            if identifier:
                return identifier
    raise ProviderError(
        f"No device called {name!r} is on the tailnet this machine can see."
    )


def reconcile_tailnet_device(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Assert HQ's decision about one device, and report what is true after.

    Only the settings HQ declares are touched. Everything else about the device
    -- its name, its tags, its routes, whether it is even switched on -- belongs
    to the machine and to whoever runs it.
    """

    del observed
    name = spec["name"]
    wanted = bool(spec.get("key_expiry_disabled"))
    current = _tailnet_device_state(name)
    if current["key_expiry_disabled"] == wanted:
        return ProviderResult(
            changed=False,
            status=current,
            conditions=[
                _condition("Ready", True, "Reconciled", "The device is as declared.")
            ],
            message="Tailnet device is current.",
        )
    if not apply:
        return ProviderResult(
            changed=True,
            status=current,
            conditions=[],
            message=(
                f"Key expiry would be {'disabled' if wanted else 'enabled'} for {name}."
            ),
        )

    identifier = _tailnet_device_id(name)
    token = _tailnet_token(spec["connection_ref"])
    request = urllib.request.Request(
        f"{TAILNET_API}/device/{urllib.parse.quote(identifier)}/key",
        data=json.dumps({"keyExpiryDisabled": wanted}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ProviderError(
                "This Tailscale credential may not change devices. It needs "
                "the devices:core scope."
            ) from exc
        raise ProviderError(
            f"Tailscale refused the change to {name} ({exc.code})."
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError("Tailscale did not answer the change request.") from exc

    return ProviderResult(
        changed=True,
        status={**current, "key_expiry_disabled": wanted, "key_expires": ""},
        conditions=[
            _condition("Ready", True, "Reconciled", "The device is as declared.")
        ],
        message=(
            f"{name} now stays on the tailnet."
            if wanted
            else f"{name} has an expiry date again."
        ),
    )


# Said once, because both calls in the approval fail the same way for the same
# reason, and an operator comparing two wordings would look for two problems.
_TAILNET_SCOPE_NEEDED = (
    "This Tailscale credential may not approve routes. It needs the devices:core scope."
)


def approve_tailnet_routes(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Approve exactly the routes this device is already advertising.

    Approval takes the whole set, so it is read before it is written: sending a
    list assembled from anywhere else would silently withdraw a route this call
    was never about. What the machine offers is what gets approved, and a
    machine offering nothing is a no-op rather than a way to clear its routes.
    """

    del observed
    name = spec["name"]
    identifier = _tailnet_device_id(name)
    token = _tailnet_token(spec.get("connection_ref", ""))
    request = urllib.request.Request(
        f"{TAILNET_API}/device/{urllib.parse.quote(identifier)}/routes",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            current = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # A refusal here is the same missing grant the write would hit, and it
        # is worth naming at the first call rather than the second: an operator
        # told only that the routes could not be read goes looking at the
        # device.
        if exc.code in (401, 403):
            raise ProviderError(_TAILNET_SCOPE_NEEDED) from exc
        raise ProviderError(f"Tailscale did not report the routes for {name}.") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"Tailscale did not report the routes for {name}.") from exc

    advertised = sorted(str(route) for route in current.get("advertisedRoutes") or ())
    enabled = sorted(str(route) for route in current.get("enabledRoutes") or ())
    pending = [route for route in advertised if route not in set(enabled)]
    status = {
        "name": name,
        "advertised_routes": advertised,
        "enabled_routes": enabled,
    }
    if not pending:
        return ProviderResult(
            changed=False,
            status=status,
            conditions=[
                _condition(
                    "Ready",
                    True,
                    "Reconciled",
                    "Every route this device advertises is approved.",
                )
            ],
            message=(
                "Nothing to approve." if advertised else f"{name} advertises no routes."
            ),
        )
    if not apply:
        return ProviderResult(
            changed=True,
            status=status,
            conditions=[],
            message=f"Would approve {', '.join(pending)} for {name}.",
        )

    request = urllib.request.Request(
        f"{TAILNET_API}/device/{urllib.parse.quote(identifier)}/routes",
        data=json.dumps({"routes": advertised}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            approved = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderError(_TAILNET_SCOPE_NEEDED) from exc
        raise ProviderError(
            f"Tailscale refused the route approval for {name}."
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"Tailscale did not answer for {name}.") from exc

    status["enabled_routes"] = sorted(
        str(route) for route in approved.get("enabledRoutes") or ()
    )
    return ProviderResult(
        changed=True,
        status=status,
        conditions=[
            _condition(
                "Ready", True, "Reconciled", "The advertised routes are approved."
            )
        ],
        message=f"Approved {', '.join(pending)} for {name}.",
    )


def _tailnet_device_state(name: str) -> dict[str, Any]:
    """What the tailnet currently says about one device."""

    for record in local_tailnet_devices():
        if record["name"] == name:
            return {
                "name": name,
                "online": record["online"],
                "key_expires": record["key_expires"],
                # No expiry is the setting, not an unknown date.
                "key_expiry_disabled": not record["key_expires"],
            }
    raise ProviderError(
        f"No device called {name!r} is on the tailnet this machine can see."
    )


def _tailnet_nodes() -> list[dict[str, Any]]:
    """The raw local reading, for the fields the record does not carry."""

    if not TAILNET_STATUS:
        raise ProviderError("This controller was not given a tailnet reading.")
    try:
        status = json.loads(Path(TAILNET_STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderError("The tailnet reading is missing or unreadable.") from exc
    return [status.get("Self") or {}, *(status.get("Peer") or {}).values()]


def _tailnet_get(token: str, path: str) -> dict[str, Any]:
    """One tailnet-level read, or nothing if it is not answerable.

    Nothing rather than an exception: these are separate facts about the same
    tailnet, and losing the whole sweep because one endpoint is unavailable to
    this credential would trade six answers for none.
    """

    request = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            found = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _tailnet_policy_etag(token: str) -> str:
    """The version of the policy HQ read, so a write cannot clobber a newer one.

    Without it, two people editing at once means the later save silently wins.
    Tailscale takes this back as ``If-Match`` and refuses the write instead.
    """

    request = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/acl",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.headers.get("etag", "")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return ""


def reconcile_tailnet_policy(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Apply the declared policy, but only if it still passes its own tests.

    The policy is the tailnet's security boundary, and its failure mode is
    locking everybody out of everything at once. Tailscale will validate a
    document on request and run the tests written inside it, so that is the
    gate: a policy whose tests fail is refused here rather than applied and
    regretted. The console warns about that; this declines.

    Conditional on the version last read, so a change made somewhere else in
    the meantime stops this rather than being overwritten by it.
    """

    del observed
    wanted = (spec.get("document") or "").strip()
    if not wanted:
        return ProviderResult(
            changed=False,
            status={},
            conditions=[],
            message="No policy is declared, so there is nothing to apply.",
        )
    try:
        document = json.loads(wanted)
    except ValueError as exc:
        raise ProviderError("The declared policy is not readable JSON.") from exc

    token = _tailnet_token(spec.get("connection_ref", ""))
    if _tailnet_policy(token) == document:
        return ProviderResult(
            changed=False,
            status={"applied": True},
            conditions=[
                _condition("Ready", True, "Reconciled", "The policy is as declared.")
            ],
            message="Tailnet policy is current.",
        )

    # The gate. Validation runs the tests the document carries, so a change
    # that would break one is refused before anything is written.
    check = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/acl/validate",
        data=json.dumps(document).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(check, timeout=30) as response:
            verdict = json.loads(response.read() or b"{}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError("Tailscale could not check the policy.") from exc
    if verdict:
        raise ProviderError(
            "The declared policy does not pass its own tests, so it was not "
            f"applied: {json.dumps(verdict)[:300]}"
        )
    if not apply:
        return ProviderResult(
            changed=True,
            status={},
            conditions=[],
            message="The policy passes its own tests and would be applied.",
        )

    etag = _tailnet_policy_etag(token)
    write = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/acl",
        data=json.dumps(document).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **({"If-Match": etag} if etag else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(write, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ProviderError(
                "The policy changed somewhere else since HQ read it, so this "
                "was not applied. Read it again and make the change on top."
            ) from exc
        raise ProviderError(f"Tailscale refused the policy ({exc.code}).") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError("Tailscale did not answer the policy write.") from exc

    return ProviderResult(
        changed=True,
        status={"applied": True},
        conditions=[
            _condition("Ready", True, "Reconciled", "The policy is as declared.")
        ],
        message="Tailnet policy applied after its own tests passed.",
    )


def _tailnet_policy(token: str) -> dict[str, Any]:
    """The tailnet's policy file, as Tailscale currently holds it."""

    request = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/acl",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            f"Tailscale refused the policy read ({exc.code}). The credential "
            "needs the policy_file scope."
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError("Tailscale did not return a readable policy.") from exc


def _who_may_reach(
    policy: dict[str, Any], token: str, target: str
) -> list[dict[str, Any]]:
    """The rules that let anything reach one address and port.

    Asked of Tailscale rather than worked out here. HQ is a reader of this
    policy and must not become a second implementation of it: an answer derived
    locally would be believed exactly as much as the real one and wrong in ways
    nobody would notice until it mattered.
    """

    request = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/acl/preview"
        f"?type=ipport&previewFor={urllib.parse.quote(target)}",
        data=json.dumps(policy).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get("matches") or []
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        # One address that cannot be previewed must not lose the others.
        return []


# The attribute an app connector is declared under. Named once: it appears in
# the policy as a key, and a second spelling of it would read as a second
# feature rather than as a typo.
_APP_CONNECTOR_ATTR = "tailscale.com/app-connectors"


def _app_connectors(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Every app connector the policy declares, as its own fact.

    An app connector is a node routing traffic for named domains on the
    tailnet's behalf, so it is a way something is reached that is neither a
    device nor a DNS record -- and it is declared inside the policy rather than
    anywhere HQ was looking.
    """

    found = []
    for attr in policy.get("nodeAttrs") or []:
        for declared in (attr.get("app") or {}).get(_APP_CONNECTOR_ATTR) or []:
            found.append(
                {
                    "name": str(declared.get("name", "")),
                    "connectors": sorted(
                        str(node) for node in declared.get("connectors") or ()
                    ),
                    "domains": sorted(
                        str(domain) for domain in declared.get("domains") or ()
                    ),
                }
            )
    return found


def list_tailnet_policy() -> list[dict[str, Any]]:
    """The policy itself: who is grouped, what is tagged, and what it grants.

    Read so HQ can show the thing its verdicts come from. A reachability answer
    an operator cannot trace to a rule is one they have to take on faith, and
    the rules are small enough to put on a page.
    """

    # Not caught here. The sweep records a raising collector as unreachable,
    # keeps what the kind last held and carries the reason. Every failure on
    # this path -- no credential rendered, a client that is not an OAuth
    # client, a refused read -- raises with its own message. Swallowed into an
    # empty list they all became the same thing: a successful sweep of a
    # tailnet with no policy, so nothing was unreachable and nothing said why.
    token = _tailnet_token("")
    policy = _tailnet_policy(token)
    return [
        {
            "record": "policy",
            # The document itself, so a declaration can hold it and be compared
            # against reality without a second read.
            "document": json.dumps(policy, indent=2, sort_keys=True),
            "settings": _tailnet_get(token, "settings"),
            "dns": {
                **_tailnet_get(token, "dns/preferences"),
                **_tailnet_get(token, "dns/nameservers"),
                **_tailnet_get(token, "dns/searchpaths"),
            },
            "groups": [
                {"name": name, "members": sorted(members)}
                for name, members in sorted((policy.get("groups") or {}).items())
            ],
            "tags": [
                {"name": name, "owners": sorted(owners)}
                for name, owners in sorted((policy.get("tagOwners") or {}).items())
            ],
            "grants": [
                {
                    "src": sorted(grant.get("src") or []),
                    "dst": sorted(grant.get("dst") or []),
                    "ip": sorted(grant.get("ip") or []),
                }
                for grant in policy.get("grants") or []
            ],
            "tests": policy.get("tests") or [],
            "lock": _tailnet_lock(),
            # A Service is a name the tailnet serves that is not a device --
            # published by whichever nodes advertise it, and reachable under
            # the policy like anything else. Nothing else in HQ would notice
            # one appearing.
            "services": [
                {
                    "name": str(service.get("name", "")),
                    "addresses": sorted(str(a) for a in service.get("addrs") or ()),
                    "comment": str(service.get("comment", "")),
                    "ports": sorted(str(p) for p in service.get("ports") or ()),
                }
                for service in (
                    _tailnet_get(token, "vip-services").get("vipServices") or []
                )
            ],
            # Not fetched: both are declared inside the policy this record
            # already carries, so reading them is reading it.
            "app_connectors": _app_connectors(policy),
            # Tailscale SSH rules are grants like any other -- who may open a
            # shell, on what, as which user -- and the grants table above shows
            # none of them because they live under their own key.
            "ssh_rules": [
                {
                    "action": str(rule.get("action", "")),
                    "src": sorted(str(s) for s in rule.get("src") or ()),
                    "dst": sorted(str(d) for d in rule.get("dst") or ()),
                    "users": sorted(str(u) for u in rule.get("users") or ()),
                }
                for rule in policy.get("ssh") or []
            ],
        }
    ]


def _reach_by_device(devices: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Who the policy lets reach each device, on the ports worth asking about.

    Swept rather than asked live, because the web process holds no credential
    and is not going to start: "who can reach this" is answered from what a
    controller already went and got, the same as every other reading on the
    page it appears on.

    Silent when there is no Tailscale credential. The devices themselves come
    from the local daemon and need none, so a controller without one still
    reports presence and simply cannot say who may reach it.
    """

    try:
        token = _tailnet_token("")
        policy = _tailnet_policy(token)
    except ProviderError:
        return {}

    # Groups are flattened here, where the policy is. HQ then answers "may this
    # device reach that one" by asking whether its identity is in a list --
    # which is reading Tailscale's answer, not re-deriving it. Expanding groups
    # in HQ would be the first step toward a second policy engine.
    members = {
        group: set(users) for group, users in (policy.get("groups") or {}).items()
    }

    def flatten(names: list[str]) -> list[str]:
        out: set[str] = set()
        for name in names:
            out.update(members.get(name, {name}))
        return sorted(out)

    asking = _ports_worth_asking()
    found: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        # IPv4 only. The policy here is written against v4, and previewing both
        # families would double every row to say the same thing twice.
        address = next((a for a in device["addresses"] if ":" not in a), "")
        if not address:
            continue
        for port in asking:
            matches = _who_may_reach(policy, token, f"{address}:{port}")
            raw = sorted(
                {name for match in matches for name in (match.get("users") or [])}
            )
            if raw:
                found.setdefault(device["name"], []).append(
                    {
                        "port": port,
                        "who": flatten(raw),
                        # The rule itself, so a verdict can show what decided it
                        # rather than only what it decided. Line numbers are the
                        # policy's own, which is how an operator finds it.
                        "rules": [
                            {
                                "who": sorted(match.get("users") or []),
                                "to": sorted(match.get("ports") or []),
                                "line": match.get("lineNumber"),
                            }
                            for match in matches
                        ],
                    }
                )
    return found


# An exit node is advertised as the two default routes rather than as a flag,
# so "does this offer to be an exit node" is a question about its route list.
_EXIT_ROUTES = frozenset({"0.0.0.0/0", "::/0"})


def _tailnet_identities(token: str) -> dict[str, dict[str, Any]]:
    """Everything the coordination server knows about each device, in one read.

    ``fields=all`` rather than a call per device. The default projection omits
    routes, so the first version of this asked the routes endpoint once per
    device and made the sweep's cost a function of how many machines exist --
    for facts that already travel in this response.

    From the API rather than the local daemon for two reasons. The daemon does
    not report tags, which is what a policy names a device by. And a peer's
    routes as the daemon sees them are the routes the ACL lets *this* node
    receive, so a route can be advertised, approved, and still absent from the
    local reading -- which makes the daemon unable to tell "never offered" from
    "offered and refused", the one distinction worth reporting.
    """

    request = urllib.request.Request(
        f"{TAILNET_API}/tailnet/-/devices?fields=all",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            devices = json.loads(response.read()).get("devices") or []
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for device in devices:
        hostname = str(device.get("hostname", ""))
        if not hostname:
            continue
        advertised = sorted(str(r) for r in device.get("advertisedRoutes") or ())
        enabled = sorted(str(r) for r in device.get("enabledRoutes") or ())
        found[hostname] = {
            "user": str(device.get("user", "")),
            "tags": sorted(device.get("tags") or []),
            "advertised_routes": advertised,
            "enabled_routes": enabled,
            # Stated separately from the route lists because it is the question
            # an operator actually asks, and because the two default routes
            # being present is not obvious as an answer to it.
            "offers_exit_node": bool(_EXIT_ROUTES & set(advertised)),
            "exit_node_approved": bool(_EXIT_ROUTES & set(enabled)),
            # Facts with no symptom until they matter. A device the tailnet has
            # not authorised is on no network; one carrying a lock error cannot
            # be reached by anything under tailnet lock; and a client left
            # behind is how a fleet acquires versions nobody chose.
            "authorized": bool(device.get("authorized", True)),
            "lock_error": str(device.get("tailnetLockError") or ""),
            "update_available": bool(device.get("updateAvailable")),
            "client_version": str(device.get("clientVersion") or ""),
            # The coordination server's own answer, rather than the daemon's
            # absence-of-an-expiry inference.
            "key_expiry_disabled": bool(device.get("keyExpiryDisabled")),
            # Three more that travel in the same response and that nothing else
            # in HQ can answer. Tailscale SSH turns a device into something the
            # policy can hand shells out on; shields-up means it accepts no
            # inbound connection at all, which looks identical to being broken;
            # and an external device belongs to somebody else's tailnet and was
            # shared into this one.
            "ssh_enabled": bool(device.get("sshEnabled")),
            "blocks_incoming": bool(device.get("blocksIncomingConnections")),
            "external": bool(device.get("isExternal")),
        }
    return found


# Where the answers start. Asking about all 65535 would be that many calls per
# device; these are the ports anything is reached on anywhere, and the rest are
# added from what this machine can actually see listening.
TAILNET_BASE_PORTS = (22, 53, 80, 443)


def _ports_worth_asking() -> tuple[int, ...]:
    """The ports something here actually listens on, plus the usual few.

    Derived rather than listed. A hardcoded set answers about ports nothing
    uses and says "cannot say" about the one an operator came to ask about --
    and the containers on this machine already state which ports they publish,
    so the set that matters is knowable rather than guessable.
    """

    found = set(TAILNET_BASE_PORTS)
    try:
        for container in list_portainer_containers():
            found.update(
                int(port)
                for port in container.get("ports") or ()
                if str(port).isdigit() and 0 < int(port) < 65536
            )
    except (ProviderError, OSError, ValueError, KeyError):
        # No Portainer, or it is not answering. The base set still applies, and
        # a sweep that reports fewer ports is better than one that reports none.
        pass
    return tuple(sorted(found))


class _ProviderRuntime:
    """Bind adapters to the controller's narrow, patchable I/O boundary."""

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return _request(url, method=method, headers=headers, payload=payload)

    def required(self, prefix: str, name: str) -> str:
        return _required(prefix, name)

    def connection_prefix(self, provider: str, connection_ref: str = "") -> str:
        return connection_prefix(provider, connection_ref)

    def condition(
        self, condition_type: str, status: bool, reason: str, message: str
    ) -> dict[str, Any]:
        return _condition(condition_type, status, reason, message)

    def ssh_connection_refs(self) -> tuple[str, ...]:
        return ssh_connection_refs()

    def ssh(
        self, connection_ref: str, operation: str, payload: bytes | None = None
    ) -> bytes:
        return _ssh(connection_ref, operation, payload)


_ADAPTER_REGISTRY = compile_controller_adapters(
    CONTROLLER_PROVIDER_ADAPTERS, _ProviderRuntime()
)


PROVIDER_INVENTORY = {
    "npm.proxy_host": list_npm,
    "cloudflare.zone": list_cloudflare_zones,
    "cloudflare.dns_record": list_cloudflare_records,
    "portainer.container": list_portainer_containers,
    "tailscale.device": list_tailnet_devices,
    "tailscale.policy": list_tailnet_policy,
    **_ADAPTER_REGISTRY.inventory,
}


# ----- Connections -----------------------------------------------------------
#
# What the controller can reach, and whether it still can. The rendered
# environment is the inventory, so this enumerates itself: a 1Password item
# becomes a row here, a probe and -- once HQ has been told -- a row on a page,
# without anything in this file naming it.


# A probe answers two questions in one call: whether the credential still works,
# and what it reaches. The second is why the connection sweep is worth running
# at all -- a Portainer knows which machines exist, a DNS token knows which zones
# it may touch, and both are facts HQ can only get by asking. Reported as names
# so every menu that offers "which machine" or "which domain" is derived from
# the credential that would have to carry out the answer.


def _probe_npm(connection_ref: str) -> dict[str, Any]:
    _npm_token(_npm_url(connection_ref), connection_ref)
    return {"detail": "Authenticated.", "reaches": []}


def _probe_cloudflare_dns(connection_ref: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {_cloudflare_token(connection_ref)}"}
    verification = _request(
        f"{_cloudflare_url(connection_ref)}/user/tokens/verify", headers=headers
    )
    if not isinstance(verification, dict) or not verification.get("success"):
        raise ProviderError("Cloudflare token verification failed.")
    # Which zones *matter* is not the controller's to know. The credential
    # reports what it can reach; HQ declares which zones it is responsible for
    # and is the only side able to compare the two.
    zones = _request(
        f"{_cloudflare_url(connection_ref)}/zones?per_page=50", headers=headers
    )
    names = sorted(
        zone["name"]
        for zone in (zones or {}).get("result", [])
        if isinstance(zone, dict) and zone.get("name")
    )
    return {"detail": f"{len(names)} zones.", "reaches": names}


def _probe_cloudflare_api(connection_ref: str) -> dict[str, Any]:
    """Prove the account credential answers, and report the sites it observes.

    ``reaches`` is the analytics sites rather than the zones, because that is
    what distinguishes this credential from the DNS one beside it: both see the
    same zones, and a page listing them twice tells an operator nothing about
    which connection is which.

    A site with no ruleset bound to a hostname is left out. Cloudflare keeps a
    Web Analytics site around after whatever it was attached to goes away, so
    the account carries entries that describe nothing -- and a site HQ cannot
    name a host for is one it could not join to anything either.
    """

    verification = _cloudflare_api_request("/user/tokens/verify", connection_ref)
    if not isinstance(verification, dict) or not verification.get("success"):
        raise ProviderError("Cloudflare token verification failed.")

    account = _analytics_account(connection_ref)
    hosts = sorted(site["host"] for site in _analytics_sites(account, connection_ref))
    measured = "site" if len(hosts) == 1 else "sites"
    return {"detail": f"{len(hosts)} analytics {measured}.", "reaches": hosts}


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
# threshold Core Web Vitals is actually defined at -- a metric passes when 75%
# of samples are good, so the 75th percentile is the number being judged.
ANALYTICS_VITALS = {
    "largest_contentful_paint_ms": "largestContentfulPaintP75",
    "interaction_to_next_paint_ms": "interactionToNextPaintP75",
    "first_contentful_paint_ms": "firstContentfulPaintP75",
    "time_to_first_byte_ms": "timeToFirstByteP75",
}

ANALYTICS_BUCKETS = ("lcp", "inp", "cls")

# Connection kinds a reading uses that no resource declares.
#
# Every other probe exists because some ProviderSpec names its provider: the
# credential is there to reconcile something, so the resource registry is the
# record of why it is carried. Analytics is observed and never reconciled --
# there is no desired state for a page view -- so nothing in that registry
# would ever name this, and without saying so here the probe would look like a
# probe for nothing. Declared rather than inferred, so a probe still cannot be
# added for a connection HQ has no stated use for.
OBSERVER_PROVIDERS = frozenset({"cloudflare_api"})


def _cloudflare_graphql(
    query: str, variables: dict[str, Any], connection_ref: str = ""
) -> dict[str, Any]:
    """One GraphQL call against the account credential.

    GraphQL answers 200 with an ``errors`` array rather than an HTTP status, so
    a caller that only checked the status would read a failed query as an empty
    estate -- which is indistinguishable from a site nobody visited.
    """

    base = _cloudflare_url(connection_ref, provider="cloudflare_api")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/graphql",
        data=body,
        method="POST",
        headers={
            "Authorization": (
                f"Bearer {_cloudflare_token(connection_ref, provider='cloudflare_api')}"
            ),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            f"Cloudflare analytics refused the query: HTTP {exc.code}."
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
        raise ProviderError(f"Cloudflare analytics rejected the query: {message}")
    return payload.get("data") or {}


def _cloudflare_api_request(path: str, connection_ref: str = "") -> Any:
    """One account-surface request through the account-scoped credential."""

    return _cloudflare_envelope(
        path, provider="cloudflare_api", connection_ref=connection_ref
    )


def _cloudflare_api_list(
    path: str, connection_ref: str = "", *, per_page: int = 100
) -> list[dict[str, Any]]:
    """Every page from one Cloudflare account list endpoint."""

    collected: list[dict[str, Any]] = []
    for page in range(1, 51):
        separator = "&" if "?" in path else "?"
        response = _cloudflare_api_request(
            f"{path}{separator}per_page={per_page}&page={page}", connection_ref
        )
        batch = (response or {}).get("result", [])
        if not isinstance(batch, list):
            raise ProviderError("Cloudflare account list returned an invalid result.")
        collected.extend(item for item in batch if isinstance(item, dict))
        total_pages = int(
            ((response or {}).get("result_info") or {}).get("total_pages") or 0
        )
        if (total_pages and page >= total_pages) or len(batch) < per_page:
            return collected
    raise ProviderError("Cloudflare account list did not terminate.")


def _analytics_account(connection_ref: str = "") -> str:
    """The one account this credential reads, discovered rather than configured."""

    accounts = _cloudflare_api_list("/accounts", connection_ref, per_page=50)
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

    result = _cloudflare_api_list(
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
    for connection_ref in provider_connection_refs("cloudflare_api"):
        account = _analytics_account(connection_ref)
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


def _probe_portainer(connection_ref: str) -> dict[str, Any]:
    environments = _portainer_environments(connection_ref)
    reachable = [item for item in environments if item["reachable"]]
    local_host = controller_id()
    return {
        "detail": (f"{len(reachable)} of {len(environments)} environments reachable."),
        "reaches": sorted(
            local_host if item["local"] and local_host else item["name"]
            for item in reachable
        ),
    }


def _human_bytes(value: int | float) -> str:
    amount = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return (
                f"{amount:.0f} {unit}"
                if unit in {"B", "KB", "MB"}
                else f"{amount:.1f} {unit}"
            )
        amount /= 1024
    return "0 B"


def _container_cpu_percent(stats: dict[str, Any]) -> float:
    cpu = stats.get("cpu_stats") or {}
    previous = stats.get("precpu_stats") or {}
    cpu_delta = float((cpu.get("cpu_usage") or {}).get("total_usage") or 0) - float(
        (previous.get("cpu_usage") or {}).get("total_usage") or 0
    )
    system_delta = float(cpu.get("system_cpu_usage") or 0) - float(
        previous.get("system_cpu_usage") or 0
    )
    online = float(
        cpu.get("online_cpus")
        or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or ())
        or 1
    )
    return (
        (cpu_delta / system_delta) * online * 100
        if cpu_delta > 0 and system_delta > 0
        else 0.0
    )


_HOST_GLANCE_SCRIPT = b"""\
import json
import os
import shutil
import time

def cpu_reading():
    with open('/proc/stat', encoding='utf-8') as source:
        values = [int(value) for value in source.readline().split()[1:]]
    return sum(values), values[3] + values[4]

before_total, before_idle = cpu_reading()
time.sleep(0.2)
after_total, after_idle = cpu_reading()
elapsed = after_total - before_total
cpu = 100 * (1 - ((after_idle - before_idle) / elapsed)) if elapsed else 0
memory = {}
with open('/proc/meminfo', encoding='utf-8') as source:
    for line in source:
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
total_memory = memory.get('MemTotal', 0)
available_memory = memory.get('MemAvailable', memory.get('MemFree', 0))
disk = shutil.disk_usage('/')
print(json.dumps({
    'cpu_percent': cpu,
    'cores': os.cpu_count() or 0,
    'load_1m': os.getloadavg()[0],
    'memory_used': max(0, total_memory - available_memory),
    'memory_total': total_memory,
    'storage_used': disk.used,
    'storage_total': disk.total,
}))
"""


def _host_glance(key: str, connection_ref: str) -> dict[str, Any]:
    reading = json.loads(_ssh(connection_ref, "python3 -", _HOST_GLANCE_SCRIPT))
    memory_used = int(reading.get("memory_used") or 0)
    memory_total = int(reading.get("memory_total") or 0)
    storage_used = int(reading.get("storage_used") or 0)
    storage_total = int(reading.get("storage_total") or 0)
    memory_percent = memory_used / memory_total * 100 if memory_total else 0
    storage_percent = storage_used / storage_total * 100 if storage_total else 0
    return {
        "key": key,
        "status": "good",
        "summary": f"Host load {float(reading.get('load_1m') or 0):.2f}",
        "metrics": [
            {
                "label": "CPU",
                "value": f"{float(reading.get('cpu_percent') or 0):.0f}%",
                "detail": f"{int(reading.get('cores') or 0)} cores",
            },
            {
                "label": "Memory",
                "value": f"{memory_percent:.0f}%",
                "detail": (
                    f"{_human_bytes(memory_used)} of {_human_bytes(memory_total)} used"
                ),
            },
            {
                "label": "Storage",
                "value": f"{storage_percent:.0f}%",
                "detail": (
                    f"{_human_bytes(storage_used)} of "
                    f"{_human_bytes(storage_total)} used on /"
                ),
            },
        ],
    }


def _portainer_glance() -> dict[str, Any]:
    refs = provider_connection_refs("portainer")
    if not refs:
        raise ProviderError("No Portainer connection was supplied.")
    machines = []
    for connection_ref in refs:
        base = _portainer_url(connection_ref)
        headers = _portainer_headers(connection_ref)
        for environment in _load_portainer_environments(connection_ref):
            if not environment["reachable"]:
                continue
            cpu_percent = 0.0
            memory_used = 0
            storage_used = 0
            running = 0
            prefix = f"{base}/endpoints/{environment['id']}/docker"
            info = _request(f"{prefix}/info", headers=headers) or {}
            disk = _request(f"{prefix}/system/df", headers=headers) or {}
            containers = (
                _request(f"{prefix}/containers/json?all=false", headers=headers) or []
            )
            cores = int(info.get("NCPU") or 0)
            memory_total = int(info.get("MemTotal") or 0)
            storage_used += int(disk.get("LayersSize") or 0)
            storage_used += sum(
                int((volume.get("UsageData") or {}).get("Size") or 0)
                for volume in disk.get("Volumes") or []
            )
            storage_used += sum(
                int(item.get("Size") or 0) for item in disk.get("BuildCache") or []
            )
            for container in containers:
                stats = (
                    _request(
                        f"{prefix}/containers/{container['Id']}/stats?stream=false&one-shot=true",
                        headers=headers,
                    )
                    or {}
                )
                memory = stats.get("memory_stats") or {}
                cache = (memory.get("stats") or {}).get("inactive_file") or 0
                memory_used += max(0, int(memory.get("usage") or 0) - int(cache))
                cpu_percent += _container_cpu_percent(stats)
                running += 1
            machine_key = (
                controller_id()
                if environment["local"] and controller_id()
                else str(environment["name"])
            )
            machines.append(
                {
                    "key": machine_key,
                    "status": "good",
                    "summary": f"{running} containers running",
                    "metrics": [
                        {
                            "label": "Container CPU",
                            "value": f"{cpu_percent:.0f}%",
                            "detail": f"{cores} cores",
                        },
                        {
                            "label": "Container memory",
                            "value": _human_bytes(memory_used),
                            "detail": f"of {_human_bytes(memory_total)} available",
                        },
                        {
                            "label": "Docker storage",
                            "value": _human_bytes(storage_used),
                            "detail": "layers, volumes, and build cache",
                        },
                    ],
                }
            )
    return {
        "panel_id": "infrastructure",
        "machines": machines,
    }


def _nws_glance(point: str) -> dict[str, Any]:
    point = point.strip()
    parts = point.split(",")
    if len(parts) != 2:
        raise ProviderError("SEVERINO_NWS_POINT must be latitude,longitude.")
    try:
        latitude, longitude = (float(part.strip()) for part in parts)
    except ValueError as exc:
        raise ProviderError("SEVERINO_NWS_POINT is not numeric.") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ProviderError("SEVERINO_NWS_POINT is outside valid coordinates.")
    headers = {
        "Accept": "application/geo+json",
        "User-Agent": "Severino-HQ/1.0 (https://github.com/jseverino/severino-hq)",
    }
    point_data = (
        _request(
            f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}",
            headers=headers,
        )
        or {}
    )
    properties = point_data.get("properties") or {}
    hourly = (
        _request(str(properties.get("forecastHourly") or ""), headers=headers) or {}
    )
    periods = (hourly.get("properties") or {}).get("periods") or []
    current = periods[0] if periods else {}
    alerts = (
        _request(
            f"https://api.weather.gov/alerts/active?point={latitude:.4f},{longitude:.4f}",
            headers=headers,
        )
        or {}
    )
    active = alerts.get("features") or []
    place = (properties.get("relativeLocation") or {}).get("properties") or {}
    location = ", ".join(
        part for part in (place.get("city"), place.get("state")) if part
    )
    status = "serious" if active else "good"
    metrics = [
        {
            "label": "Now",
            "value": str(current.get("shortForecast") or "Unavailable"),
            "detail": str(current.get("name") or ""),
        },
        {
            "label": "Temperature",
            "value": f"{current.get('temperature', '—')}°{current.get('temperatureUnit', 'F')}",
            "detail": str(current.get("windChill") or ""),
        },
        {
            "label": "Wind",
            "value": f"{current.get('windDirection', '')} {current.get('windSpeed', '—')}".strip(),
            "detail": "NWS hourly forecast",
        },
    ]
    if active:
        metrics.append(
            {
                "label": "Alerts",
                "value": str(len(active)),
                "detail": "active for this point",
            }
        )
    return {
        "panel_id": "weather",
        "point": f"{latitude:.4f},{longitude:.4f}",
        "status": status,
        "summary": location or "National Weather Service",
        "metrics": metrics,
    }


def _infrastructure_glance(targets: list[dict[str, Any]]) -> dict[str, Any]:
    available_ssh = set(ssh_connection_refs())
    unresolved = [
        target
        for target in targets
        if not any(str(ref) in available_ssh for ref in target.get("connections") or [])
    ]
    docker_by_key = {}
    if unresolved:
        portainer = _portainer_glance()
        docker_by_key = {
            str(item.get("key", "")): item for item in portainer.get("machines", [])
        }
    machines = []
    for target in targets:
        key = str(target.get("key", "")).strip()
        connection_ref = next(
            (
                str(ref)
                for ref in target.get("connections") or []
                if str(ref) in available_ssh
            ),
            "",
        )
        if connection_ref:
            machines.append(_host_glance(key, connection_ref))
        elif key in docker_by_key:
            machines.append(docker_by_key[key])
        else:
            machines.append(
                {
                    "key": key,
                    "status": "attention",
                    "summary": "No host telemetry connection is available.",
                    "metrics": [],
                }
            )
    return {"panel_id": "infrastructure", "machines": machines}


def dashboard_glance(plan: dict[str, Any]) -> list[dict[str, Any]]:
    readings = []
    panel_ids = plan.get("panels") if isinstance(plan, dict) else []
    targets = plan.get("targets") if isinstance(plan, dict) else {}
    for panel_id in panel_ids or []:
        try:
            if panel_id == "infrastructure":
                readings.append(_infrastructure_glance(targets.get(panel_id) or []))
            elif panel_id == "weather":
                readings.append(
                    _nws_glance(str((targets.get("weather") or {}).get("point", "")))
                )
        except (ProviderError, OSError, ValueError, KeyError) as exc:
            failure = {
                "status": "serious",
                "summary": f"Refresh failed ({type(exc).__name__}).",
                "metrics": [],
            }
            readings.append(
                {
                    "panel_id": panel_id,
                    "machines": [{"key": controller_id(), **failure}],
                }
                if panel_id == "infrastructure"
                else {
                    "panel_id": panel_id,
                    "point": str((targets.get("weather") or {}).get("point", "")),
                    **failure,
                }
            )
    return readings


def _probe_tailscale(connection_ref: str) -> dict[str, Any]:
    """Prove the OAuth client is accepted without retaining its access token."""

    _tailnet_token(connection_ref)
    return {"detail": "OAuth credential accepted.", "reaches": []}


def _probe_ssh(connection_ref: str) -> dict[str, Any]:
    _ssh(connection_ref, "preflight")
    transport = _transport(connection_ref)
    return {
        "detail": f"{transport['user']}@{transport['host']}:{transport['port']}",
        "reaches": [transport["host"]],
    }


_CONNECTION_PROBES = {
    "npm": _probe_npm,
    "cloudflare_dns": _probe_cloudflare_dns,
    "cloudflare_api": _probe_cloudflare_api,
    "portainer": _probe_portainer,
    "tailscale": _probe_tailscale,
    **_ADAPTER_REGISTRY.connection_probes,
}

_DEFAULT_CONNECTION_ENDPOINTS = {"tailscale": TAILNET_API}


def _endpoint(prefix: str, provider: str) -> str:
    """Where a connection points, from whichever values its projection produced.

    Never a secret: a URL and a host are what an operator needs to recognise
    which of two connections they are looking at, and both are already visible
    to anyone who can reach the thing at all.
    """

    for name in ("URL", "DIRECTORY_URL"):
        url = os.environ.get(f"{prefix}_{name}", "").strip()
        if url:
            return url
    host = os.environ.get(f"{prefix}_HOST", "").strip()
    port = os.environ.get(f"{prefix}_PORT", "").strip()
    if host:
        return f"{host}:{port}" if port else host
    return _DEFAULT_CONNECTION_ENDPOINTS.get(provider, "")


def connections() -> list[dict[str, Any]]:
    """Every connection the environment carries, and whether it answers.

    One failure is that connection's failure. Reported rather than raised so a
    Cloudflare token that expired does not also make the two machines HQ can
    still reach look like they have gone away -- the sweep is the only thing
    that tells an operator which of the two happened.
    """

    ssh_refs = set(ssh_connection_refs())
    reported: list[dict[str, Any]] = []
    for connection_ref, prefix in sorted(connection_prefixes().items()):
        provider = connection_provider(connection_ref)
        probe = _CONNECTION_PROBES.get(provider)
        if probe is None and connection_ref in ssh_refs:
            # A transport, whatever its prefix happens to spell. The projection
            # that produced a host and a user is what makes it one, so this
            # stays true for a machine added under any name.
            probe = _probe_ssh
            provider = os.environ.get(f"{prefix}_PROVIDER", "").strip() or "ssh"
        connection = {
            "connection_ref": connection_ref,
            "provider": provider,
            "endpoint": _endpoint(prefix, provider),
            "probed": probe is not None,
            "ok": True,
            "detail": "",
            "reaches": [],
        }
        if probe is None:
            # Carried, usable, and not something this knows how to ask. Reported
            # as unprobed rather than omitted: a connection HQ cannot see is one
            # an operator will keep re-adding.
            connection["detail"] = "No probe for this kind of connection."
        else:
            try:
                result = probe(connection_ref)
                connection["detail"] = result["detail"]
                connection["reaches"] = result["reaches"]
            except (ProviderError, OSError, ValueError, KeyError) as exc:
                connection["ok"] = False
                connection["detail"] = str(exc)
        reported.append(connection)
    return reported


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


def _refuses(reason: str):
    """A handler for an action the registry says this controller will not take.

    The reason is the registry's, not a second copy of it here. A locked action
    that reached a controller is a bug in whatever queued it, and the operator
    reading the failure should be told the same thing the declaration form told
    them.
    """

    def locked(
        spec: dict[str, Any],
        *,
        apply: bool,
        observed: dict[str, Any] | None = None,
    ) -> ProviderResult:
        del spec, apply, observed
        raise ProviderError(reason)

    return locked


# Locked actions are generated, so a provider declared as locked needs no entry
# here at all -- and cannot be declared locked while quietly having a handler
# that acts.
PROVIDER_ACTIONS = {
    **{
        (kind, action): _refuses(policy.reason)
        for kind, capability in controller_capability_registry().capabilities.items()
        for action, policy in capability.actions.items()
        if policy.mode == "locked"
    },
    ("portainer.stack", "reconcile"): reconcile_portainer,
    ("portainer.stack", "delete"): delete_portainer,
    ("portainer.container", "restart"): restart_portainer_container,
    ("portainer.container", "start"): start_portainer_container,
    ("portainer.container", "stop"): stop_portainer_container,
    ("tls.uploaded_certificate", "reconcile"): reconcile_uploaded_certificate,
    ("tls.uploaded_certificate", "delete"): delete_uploaded_certificate,
    ("npm.proxy_host", "reconcile"): reconcile_npm,
    ("npm.proxy_host", "delete"): delete_npm,
    ("cloudflare.dns_record", "reconcile"): reconcile_cloudflare_record,
    ("cloudflare.dns_record", "delete"): delete_cloudflare_record,
    ("tls.certificate", "reconcile"): _tls_reconcile,
    ("tls.certificate", "renew"): _tls_renew,
    ("tailscale.device", "reconcile"): reconcile_tailnet_device,
    ("tailscale.device", "approve-routes"): approve_tailnet_routes,
    ("tailscale.policy", "reconcile"): reconcile_tailnet_policy,
    **_ADAPTER_REGISTRY.actions,
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
    return handler(
        resource["spec"], apply=apply, observed=resource.get("observed") or {}
    )
