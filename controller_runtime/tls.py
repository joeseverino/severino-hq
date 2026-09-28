"""Certificates: observing them where they are served, issuing, deploying and verifying."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from application.expiry import days_until
from control_plane.providers import controller_capability_registry
from control_plane.names import certificate_covers
from control_plane.provider_adapters.tls import CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND
from control_plane.provider_adapters.contracts import (
    ProviderError,
    ProviderResult,
)
from control_plane.provider_adapters import npm, onepassword
from .handlers import acts
from . import cloudflare, commands, connection_env, provider_http, provider_runtime


def _npm_url(connection_ref: str = "") -> str:
    return npm.url(provider_runtime._RUNTIME, connection_ref)


def _npm_token(base_url: str, connection_ref: str = "") -> str:
    return npm.token(provider_runtime._RUNTIME, base_url, connection_ref)


# The port every TLS reading is taken on. Named because what was tried is
# reported when a reading fails, and a bare 443 in two places drifts.
TLS_PORT = 443


def _observe_tls_domain(
    domain: str, *, connect_host: str | None = None
) -> dict[str, Any]:
    try:
        tls_context = provider_http._tls_context()
        with socket.create_connection(
            (connect_host or domain, TLS_PORT), timeout=15
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
            provider_http._required(connection_env.connection_prefix("npm"), "URL")
        ).hostname
        if not hostname:
            raise ProviderError("NPM origin verification endpoint is missing.")
        return hostname
    if kind in {"caddy", "cpanel"}:
        transport = connection_env._transport(consumer["connection_ref"])
        hostname = transport.get("host")
        if not hostname:
            raise ProviderError(f"{kind} origin verification endpoint is missing.")
        return hostname
    return None


def _npm_covered_hosts(certificate_domains: list[str]) -> list[dict[str, Any]]:
    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    hosts = provider_http._request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    names = set(certificate_domains)
    return [
        host
        for host in hosts
        if host.get("enabled") is not False
        and any(
            certificate_covers(domain, names) for domain in host.get("domain_names", [])
        )
    ]


def _tls_consumer_domains(consumer: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """The names to read one consumer on: declared, plus what NPM routes."""

    domains = list(consumer.get("verify_domains", []))
    if consumer["kind"] == "npm" and consumer.get("discover_covered_hosts"):
        covered = _npm_covered_hosts(spec["domains"])
        domains = sorted(
            {*domains, *(name for host in covered for name in host.get("domain_names", []))}
        )
    return domains


def _tls_unreachable(
    consumer: dict[str, Any], domain: str, endpoint: str, exc: Exception
) -> dict[str, str]:
    """A consumer that could not be read, with where the reading was tried."""

    return {
        "consumer": consumer["name"],
        "domain": domain,
        "endpoint": endpoint,
        "port": str(TLS_PORT),
        "reason": str(exc),
    }


def _read_tls_consumer(
    consumer: dict[str, Any], domains: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read each domain on one consumer: (observations, unreachable)."""

    try:
        connect_host = _consumer_tls_endpoint(consumer)
    except ProviderError as exc:
        return [], [_tls_unreachable(consumer, "", "", exc)]
    observations: list[dict[str, Any]] = []
    unreachable: list[dict[str, str]] = []
    for domain in domains:
        try:
            observed = _observe_tls_domain(domain, connect_host=connect_host)
        except ProviderError as exc:
            unreachable.append(
                _tls_unreachable(consumer, domain, connect_host or domain, exc)
            )
            continue
        observed["consumer"] = consumer["name"]
        observed["consumer_kind"] = consumer["kind"]
        observations.append(observed)
    return observations, unreachable


def _tls_conditions(
    spec: dict[str, Any],
    observations: list[dict[str, Any]],
    unverified: list[str],
    unreachable: list[dict[str, str]],
    days_remaining: int,
) -> list[dict[str, Any]]:
    """What the reading says about the certificate, Ready when nothing else."""

    conditions: list[dict[str, Any]] = []
    if len({item["fingerprint_sha256"] for item in observations}) > 1:
        conditions.append(
            provider_http._condition(
                "Drifted",
                True,
                "ConsumerMismatch",
                "TLS consumers are serving different certificates.",
            )
        )
    if days_remaining <= spec["renewal_window_days"]:
        conditions.append(
            provider_http._condition(
                "Degraded",
                True,
                "ExpiringSoon",
                f"A verified TLS consumer expires in {days_remaining} days.",
            )
        )
    if unverified:
        conditions.append(
            provider_http._condition(
                "Degraded",
                True,
                "ConsumerUnverified",
                "No verification domain is declared for: " + ", ".join(unverified),
            )
        )
    if unreachable:
        conditions.append(
            provider_http._condition(
                "Degraded",
                True,
                "ConsumerUnreachable",
                "Could not be read: "
                + ", ".join(item["domain"] or item["consumer"] for item in unreachable),
            )
        )
    return conditions or [
        provider_http._condition("Ready", True, "Verified", "All TLS consumers are current.")
    ]


def reconcile_tls(spec: dict[str, Any]) -> ProviderResult:
    observations: list[dict[str, Any]] = []
    unverified_consumers: list[str] = []
    # Each consumer that could not be read, with the address and port the
    # reading was attempted against. What was tried is the part that decides
    # what to do about it, and it is known here and nowhere else: the endpoint
    # is resolved from a connection only the controller holds.
    unreachable: list[dict[str, str]] = []
    for consumer in spec["consumers"]:
        domains = _tls_consumer_domains(consumer, spec)
        if not domains:
            unverified_consumers.append(consumer["name"])
            continue
        # One consumer that cannot be reached is a fact about that consumer.
        # Reported against it and the sweep carries on, so the certificate
        # still says what every other consumer is serving and the facts it
        # publishes are still written. A single unreachable host otherwise
        # decides what is known about all of them.
        read, missed = _read_tls_consumer(consumer, domains)
        observations.extend(read)
        unreachable.extend(missed)

    if not observations:
        # Nothing was read, so there is no expiry, no fingerprint and nothing to
        # compare. Which of the two it is decides what an operator does next.
        if unreachable:
            raise ProviderError(
                "No TLS consumer could be reached: "
                + "; ".join(item["reason"] for item in unreachable)
            )
        raise ProviderError("No TLS verification domains were declared.")
    soonest = min(datetime.fromisoformat(item["not_after"]) for item in observations)
    newest = max(observations, key=lambda item: item["not_after"])
    conditions = _tls_conditions(
        spec, observations, unverified_consumers, unreachable, days_until(soonest)
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
            # Named beside the ones that answered, so the page can say which
            # consumers this reading covers and which it does not.
            "unreachable_consumers": unreachable,
        },
        conditions=conditions,
        message="TLS consumers observed.",
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
        cert_pub = commands._run(
            ["openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout"],
            step="reading the certificate",
        )
        key_pub = commands._run(
            ["openssl", "pkey", "-in", str(key_path), "-pubout"],
            step="reading the private key",
        )
        if cert_pub != key_pub:
            raise ProviderError("Certificate and private key do not match.")
        fingerprint = (
            commands._run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(cert_path),
                    "-noout",
                    "-fingerprint",
                    "-sha256",
                ],
                step="reading the certificate fingerprint",
            )
            .decode()
            .strip()
            .split("=", 1)[-1]
            .replace(":", "")
            .lower()
        )
        san_output = commands._run(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-ext",
                "subjectAltName",
            ],
            step="reading the certificate names",
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


def _foreign_acme_entry(acme_dir: Path) -> str:
    """The first entry in the ACME state this process could not take ownership of.

    Certbot saves a renewal by copying the previous key's owner and group onto
    the new one. A process that is not root can only chown to itself, so one
    file carrying another group is enough to fail the save: after the CA has
    already issued, which spends a certificate against its rate limit and leaves
    an orphaned key behind. Checked before asking the CA for anything.
    """

    uid, gid = os.getuid(), os.getgid()
    for root, directories, files in os.walk(acme_dir):
        for name in (*directories, *files):
            path = Path(root, name)
            try:
                stat = path.lstat()
            except OSError:
                continue
            if stat.st_uid != uid or stat.st_gid != gid:
                return (
                    f"{path.relative_to(acme_dir)} is owned "
                    f"{stat.st_uid}:{stat.st_gid}, not {uid}:{gid}"
                )
    return ""


def _issue_certificate(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    acme_dir = Path(provider_http._required("HQ", "ACME_DIR"))
    if not acme_dir.is_dir() or not os.access(acme_dir, os.W_OK):
        raise ProviderError("ACME state directory is not writable.")
    foreign = _foreign_acme_entry(acme_dir)
    if foreign:
        raise ProviderError(
            f"ACME state is not wholly the controller's: {foreign}. Certbot "
            "would be issued a certificate it cannot save, so nothing was requested."
        )
    commands._run(["certbot", "--version"], step="certbot preflight")
    credentials = acme_dir / "cloudflare.ini"
    credentials.write_text("dns_cloudflare_api_token = " + cloudflare._cloudflare_token() + "\n")
    credentials.chmod(0o600)
    command = [
        "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--email",
        provider_http._required("ACME", "EMAIL"),
        "--server",
        provider_http._required("ACME", "DIRECTORY_URL"),
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
        commands._run(command, step="certbot certonly")
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
        Path(provider_http._required("HQ", "ACME_DIR")) / "config" / "live" / spec["certificate_name"]
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
            commands._run(
                ["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"],
                step="openssl read lineage expiry",
            )
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


_NPM_CERTIFICATE_IDS = "npm_certificate_ids"


def _npm_certificate_name(consumer: dict[str, Any]) -> str:
    """The display name HQ gives the NPM certificate it installs for a consumer."""

    return f"Severino HQ - {consumer['name']}"


def _npm_certificate_ids(
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
    reported = observed.get(_NPM_CERTIFICATE_IDS)
    ids = {
        str(name): value
        for name, value in (reported.items() if isinstance(reported, dict) else ())
        if type(value) is int
    }
    single = observed.get("npm_certificate_id")
    if not ids and len(consumers) == 1 and type(single) is int:
        ids = {consumers[0]: single}
    return {name: ids[name] for name in consumers if name in ids}


def _with_npm_certificate_ids(
    result: ProviderResult, known: dict[str, int]
) -> ProviderResult:
    """Carry the installed certificate ids into a report that did not install one."""

    if not known or _NPM_CERTIFICATE_IDS in result.status:
        return result
    return ProviderResult(
        changed=result.changed,
        status={
            **result.status,
            _NPM_CERTIFICATE_IDS: known,
            "npm_certificate_id": list(known.values())[-1],
        },
        conditions=result.conditions,
        message=result.message,
    )


def _npm_managed_certificate(
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

    base_url = _npm_url()
    headers = {"Authorization": f"Bearer {_npm_token(base_url)}"}
    nice_name = _npm_certificate_name(consumer)
    certificates = provider_http._request(f"{base_url}/nginx/certificates", headers=headers)
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
        certificate = provider_http._request(
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
    provider_http._multipart_request(
        f"{base_url}/nginx/certificates/validate",
        headers=headers,
        files=files,
    )
    provider_http._multipart_request(
        f"{base_url}/nginx/certificates/{certificate_id}/upload",
        headers=headers,
        files=files,
    )
    hosts = provider_http._request(f"{base_url}/nginx/proxy-hosts", headers=headers)
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
        provider_http._request(
            f"{base_url}/nginx/proxy-hosts/{host['id']}",
            method="PUT",
            headers=headers,
            payload={"certificate_id": certificate_id},
        )
    return certificate_id, {"nice_name": nice_name}


def _cpanel_sites(consumer: dict[str, Any]) -> list[str]:
    """The cPanel sites this consumer installs on, decided before anything is issued.

    cPanel holds one certificate per *site*, and every alias of a site serves
    whatever that site holds. So the question is never "which names", it is
    "which sites serve the names HQ will check". The account is asked for its
    sites and their names, and the answer has to cover every verified name:

    - with `install_domains` declared, the sites serving those names;
    - without, every site that serves a verified name.

    A verified name no chosen site serves is refused here, by name, before any
    certificate is requested. So is a name the account does not serve at all.
    """

    try:
        answer = json.loads(commands._ssh(consumer["connection_ref"], "sites") or b"{}")
    except ValueError as exc:
        raise ProviderError(
            f"{consumer['name']} returned a site list HQ could not read."
        ) from exc
    sites = answer.get("sites") if isinstance(answer, dict) else None
    if not isinstance(sites, dict) or not sites:
        raise ProviderError(f"{consumer['name']} reported no sites.")
    site_of = {
        name.lower(): site
        for site, names in sites.items()
        for name in (site, *(names or ()))
    }
    verify = sorted({name.lower() for name in consumer.get("verify_domains", ())})
    declared = sorted({name.lower() for name in consumer.get("install_domains", ())})

    not_hosted = sorted(name for name in (*verify, *declared) if name not in site_of)
    if not_hosted:
        raise ProviderError(
            f"{consumer['name']} does not serve "
            + ", ".join(not_hosted)
            + ". Remove the name from the target, or add it to the hosting account."
        )
    chosen = sorted({site_of[name] for name in (declared or verify)})
    served = {name.lower() for site in chosen for name in (site, *sites[site])}
    unserved = [name for name in verify if name not in served]
    if unserved:
        raise ProviderError(
            f"{consumer['name']} would be checked at "
            + ", ".join(unserved)
            + " but installs only on "
            + ", ".join(chosen)
            + ". Add those names to the target's install list, or leave the list "
            "empty to install on every site that serves a checked name."
        )
    if not chosen:
        raise ProviderError(f"{consumer['name']} has no site to install on.")
    return chosen


def _plan_deployment(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Everything a deploy needs to know from the consumers, asked up front.

    Runs before a certificate is requested and before any consumer is touched, so
    a target that cannot be satisfied costs nothing: no issuance against the CA's
    rate limit, no half-deployed estate, no rollback.
    """

    return {
        consumer["name"]: _cpanel_sites(consumer)
        for consumer in spec["consumers"]
        if consumer["kind"] == "cpanel"
    }


def _deploy_certificate(
    spec: dict[str, Any],
    fullchain: bytes,
    private_key: bytes,
    plan: dict[str, list[str]],
    known: dict[str, int] | None = None,
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
                    consumer,
                    spec["domains"],
                    fullchain,
                    private_key,
                    (known or {}).get(consumer["name"]),
                )
                deployment_status.update(
                    npm_certificate_id=certificate_id,
                    npm_certificate_identity=identity,
                )
                deployment_status.setdefault(_NPM_CERTIFICATE_IDS, {})[
                    consumer["name"]
                ] = certificate_id
            elif consumer["kind"] == "caddy":
                commands._ssh(consumer["connection_ref"], "deploy", bundle)
            elif consumer["kind"] == "cpanel":
                # One login for every site, and the account reports each one.
                sites = plan[consumer["name"]]
                payload = json.dumps(
                    {
                        "sites": sites,
                        "cert": leaf.decode(),
                        "key": private_key.decode(),
                        "cabundle": chain.decode(),
                    },
                    separators=(",", ":"),
                ).encode()
                commands._ssh(consumer["connection_ref"], "deploy", payload)
                deployment_status.setdefault("cpanel_sites", {})[
                    consumer["name"]
                ] = sites
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
            stale: dict[str, list[str]] = {}
            for item in evidence:
                if not item["matches_expected"]:
                    stale.setdefault(item["consumer"], []).append(item["domain"])
            # Which consumer and which names, in the message itself.
            detail = "; ".join(
                f"{consumer} still serves the previous certificate at "
                + ", ".join(sorted(names))
                for consumer, names in sorted(stale.items())
            )
            raise ProviderError(
                f"{len(stale)} of {len(spec['consumers'])} TLS consumers "
                f"did not activate the certificate within {timeout}s: {detail}.",
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
    plan: dict[str, list[str]],
    artifact_source: str,
    reason: str,
    message: str,
    known: dict[str, int] | None = None,
) -> ProviderResult:
    expected_fingerprint = _validate_certificate(
        fullchain, private_key, spec["domains"]
    )
    try:
        deployment_status = _deploy_certificate(
            spec, fullchain, private_key, plan, known
        )
        observed = _verify_tls_deployment(spec, expected_fingerprint)
    except ProviderError as exc:
        try:
            _deploy_certificate(spec, previous_fullchain, previous_key, plan, known)
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
            provider_http._condition(
                "Ready", True, reason, "All TLS consumers serve the certificate."
            )
        ],
        message=message,
    )


def _lineage(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    lineage = (
        Path(provider_http._required("HQ", "ACME_DIR")) / "config" / "live" / spec["certificate_name"]
    )
    try:
        return lineage.joinpath("fullchain.pem").read_bytes(), lineage.joinpath(
            "privkey.pem"
        ).read_bytes()
    except OSError as exc:
        raise ProviderError(
            "Certbot lineage is unavailable for reconciliation."
        ) from exc


def apply_tls_reconcile(
    spec: dict[str, Any], *, known: dict[str, int] | None = None
) -> ProviderResult:
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
                provider_http._condition("Ready", True, "Verified", "All TLS consumers match.")
            ],
            message="Certificate consumers already match the managed lineage.",
        )
    caddy = next((item for item in spec["consumers"] if item["kind"] == "caddy"), None)
    if caddy is None:
        raise ProviderError("Certificate reconciliation requires a rollback source.")
    plan = _plan_deployment(spec)
    previous_fullchain, previous_key = _read_bundle(
        commands._ssh(caddy["connection_ref"], "snapshot")
    )
    return _deploy_tls_transaction(
        spec,
        fullchain,
        private_key,
        previous_fullchain,
        previous_key,
        plan=plan,
        artifact_source="existing_lineage",
        reason="Reconciled",
        message="Certificate redistributed and verified without issuance.",
        known=known,
    )


def renew_tls(
    spec: dict[str, Any], *, known: dict[str, int] | None = None
) -> ProviderResult:
    caddy = next((item for item in spec["consumers"] if item["kind"] == "caddy"), None)
    if caddy is None:
        raise ProviderError("Certificate renewal requires a rollback source.")
    # Before the CA is asked for anything: a target that cannot be satisfied
    # should cost nothing.
    plan = _plan_deployment(spec)
    previous_fullchain, previous_key = _read_bundle(
        commands._ssh(caddy["connection_ref"], "snapshot")
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
        plan=plan,
        artifact_source=artifact_source,
        reason="Renewed",
        message="Certificate renewed, deployed, and verified.",
        known=known,
    )


def _lineage_material(spec: dict[str, Any]) -> Callable[[], tuple[bytes, bytes]]:
    """Read this certificate's own material, when and only when it is wanted.

    Returned rather than read, so the ordinary pass (everything already where
    it should be) never opens a private key at all. The publisher calls it only
    once it has established that what is filed is a different certificate.

    The lineage on disk is the same one the deploy path installs from, so what is
    filed is what is served rather than a second rendering of it.
    """

    def read() -> tuple[bytes, bytes]:
        lineage = (
            Path(provider_http._required("HQ", "ACME_DIR"))
            / "config"
            / "live"
            / spec["certificate_name"]
        )
        return (
            lineage.joinpath("fullchain.pem").read_bytes(),
            lineage.joinpath("privkey.pem").read_bytes(),
        )

    return read


def _publish_tls_facts(
    spec: dict[str, Any], result: ProviderResult, *, apply: bool
) -> ProviderResult:
    """Record what was just observed wherever the certificate says to record it.

    Runs after the certificate's own work and can only add to its report. A
    failure here is reported and then let go: publishing facts is a convenience
    for whoever opens the item next, and the certificate being installed and
    serving is the job. Raising would turn a password manager being unreachable
    into a certificate that failed to reconcile, and then into an automatic
    retry of a deployment that had nothing wrong with it.

    No condition is raised either, for the same reason: a `Degraded` on the
    certificate says the certificate is degraded, and this says a note about it
    was not filed. It goes in the status and in the message, where an operator
    reading the operation sees it.

    Nothing is written on a dry run. Being asked what a reconcile *would* do is
    not permission to change something outside HQ.
    """

    publications = spec.get("publish_to") or ()
    if not apply or not publications:
        return result
    desired = onepassword.facts(spec, result.status)
    published: list[dict[str, Any]] = []
    for publication in publications:
        if not desired:
            published.append(
                {
                    "target": publication["name"],
                    "written": False,
                    "detail": (
                        "HQ has no single fingerprint for this certificate yet."
                    ),
                }
            )
            continue
        try:
            published.append(
                onepassword.publish(
                    provider_runtime._RUNTIME, publication, desired, _lineage_material(spec)
                )
            )
        except (ProviderError, OSError, ValueError) as exc:
            # The message, not the exception type: `ProviderError` is written to
            # carry no credential material, and an item name is HQ's own.
            published.append(
                {"target": publication["name"], "written": False, "detail": str(exc)}
            )
    unfiled = [item["target"] for item in published if not item["written"]]
    return ProviderResult(
        changed=result.changed,
        status={**result.status, "published_facts": published},
        conditions=result.conditions,
        message=(
            f"{result.message} Facts were not recorded on: {', '.join(unfiled)}."
            if unfiled
            else result.message
        ),
    )


@acts(CERTIFICATE_KIND, "reconcile")
def _tls_reconcile(
    spec: dict[str, Any],
    *,
    apply: bool,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    known = _npm_certificate_ids(spec, observed)
    result = apply_tls_reconcile(spec, known=known) if apply else reconcile_tls(spec)
    return _with_npm_certificate_ids(
        _publish_tls_facts(spec, result, apply=apply), known
    )


@acts(CERTIFICATE_KIND, "renew")
def _tls_renew(
    spec: dict[str, Any],
    *,
    apply: bool,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    if apply:
        # A renewal is the moment the facts actually change (a new expiry and a
        # new fingerprint) so it is the one an item most needs to hear about.
        known = _npm_certificate_ids(spec, observed)
        return _with_npm_certificate_ids(
            _publish_tls_facts(spec, renew_tls(spec, known=known), apply=True), known
        )
    return ProviderResult(
        changed=True,
        status={},
        conditions=[],
        message="Certificate would be issued, deployed, verified, and rolled back on failure.",
    )


@acts(UPLOADED_CERTIFICATE_KIND, "reconcile")
def reconcile_uploaded_certificate(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Install a certificate HQ was given rather than one it issued.

    Deployment is identical (a proxy does not care which authority signed the
    thing it serves) so this reuses the same path as a renewal and differs
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
                provider_http._condition("Ready", True, "Planned", "Would install the certificate.")
            ],
            message="Would install the stored certificate.",
        )
    target = {
        "certificate_name": spec["certificate_name"],
        "domains": domains,
        "consumers": spec["consumers"],
    }
    deployment = _deploy_certificate(
        target,
        fullchain.encode(),
        private_key.encode(),
        _plan_deployment(target),
        _npm_certificate_ids(spec, observed),
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
            provider_http._condition("Ready", True, "Installed", "Stored certificate installed.")
        ],
        message="Stored certificate installed.",
    )


@acts(UPLOADED_CERTIFICATE_KIND, "delete")
def delete_uploaded_certificate(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Remove an installed certificate, or refuse and say who has to do it.

    Only Nginx Proxy Manager can be undone from here. A Caddy target receives a
    certificate over an SSH forced command that implements ``deploy`` and
    nothing else, so removing one means editing the remote side, and a delete
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
    certificates = provider_http._request(f"{base_url}/nginx/certificates", headers=headers)
    installed = set(_npm_certificate_ids(spec, observed).values())
    matches = [item for item in certificates if item.get("id") in installed]
    if not matches:
        # A display name is not an identity: NPM lets anyone set one. A
        # certificate HQ holds no id for is left for an operator to judge.
        wanted = {_npm_certificate_name(consumer) for consumer in spec["consumers"]}
        named = sorted(
            str(item.get("nice_name"))
            for item in certificates
            if item.get("nice_name") in wanted
        )
        if named:
            raise ProviderError(
                "NPM holds " + ", ".join(named) + ", but HQ has no record of "
                "installing it, so it was not removed. Remove it in NPM if it "
                "is HQ's, then remove this again."
            )
        return ProviderResult(
            changed=False,
            status={"removed": True},
            conditions=[
                provider_http._condition("Ready", True, "Absent", "No such certificate in NPM.")
            ],
            message="Certificate was already absent from NPM.",
        )

    # A certificate still bound to a proxy host cannot be deleted without taking
    # TLS down on it. Naming the hosts is the actionable part: the operator has
    # to point them at something else first.
    hosts = provider_http._request(f"{base_url}/nginx/proxy-hosts", headers=headers)
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
            provider_http._request(
                f"{base_url}/nginx/certificates/{item['id']}",
                method="DELETE",
                headers=headers,
            )
    return ProviderResult(
        changed=True,
        status={"removed": True},
        conditions=[
            provider_http._condition("Ready", True, "Removed", "Certificate removed from NPM.")
        ],
        message="Certificate removed from NPM.",
    )
