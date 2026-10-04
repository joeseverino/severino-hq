"""Verifying a deployed certificate.

What each consumer actually serves on its TLS port, judged against what was
deployed, and the conditions a certificate reports from that.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from application.expiry import days_until
from control_plane.providers import controller_capability_registry
from control_plane.names import certificate_covers
from control_plane.provider_adapters.tls import CERTIFICATE_KIND
from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from . import connection_env, npm_certificates, provider_http


# The port every TLS reading is taken on. Named because what was tried is
# reported when a reading fails, and a bare 443 in two places drifts.
TLS_PORT = 443


def _observe_tls_domain(
    domain: str, *, connect_host: str | None = None
) -> dict[str, Any]:
    try:
        tls_context = provider_http.tls_context()
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
            provider_http.required(connection_env.connection_prefix("npm"), "URL")
        ).hostname
        if not hostname:
            raise ProviderError("NPM origin verification endpoint is missing.")
        return hostname
    if kind in {"caddy", "cpanel"}:
        transport = connection_env.ssh_target(consumer["connection_ref"])
        hostname = transport.get("host")
        if not hostname:
            raise ProviderError(f"{kind} origin verification endpoint is missing.")
        return hostname
    return None


def _npm_covered_hosts(certificate_domains: list[str]) -> list[dict[str, Any]]:
    base_url = npm_certificates.npm_url()
    headers = {"Authorization": f"Bearer {npm_certificates.npm_token(base_url)}"}
    hosts = provider_http.request_json(f"{base_url}/nginx/proxy-hosts", headers=headers)
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
            provider_http.condition(
                "Drifted",
                True,
                "ConsumerMismatch",
                "TLS consumers are serving different certificates.",
            )
        )
    if days_remaining <= spec["renewal_window_days"]:
        conditions.append(
            provider_http.condition(
                "Degraded",
                True,
                "ExpiringSoon",
                f"A verified TLS consumer expires in {days_remaining} days.",
            )
        )
    if unverified:
        conditions.append(
            provider_http.condition(
                "Degraded",
                True,
                "ConsumerUnverified",
                "No verification domain is declared for: " + ", ".join(unverified),
            )
        )
    if unreachable:
        conditions.append(
            provider_http.condition(
                "Degraded",
                True,
                "ConsumerUnreachable",
                "Could not be read: "
                + ", ".join(item["domain"] or item["consumer"] for item in unreachable),
            )
        )
    return conditions or [
        provider_http.condition("Ready", True, "Verified", "All TLS consumers are current.")
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


def tls_consumers_serve(
    spec: dict[str, Any], status: dict[str, Any], expected_fingerprint: str
) -> bool:
    """Whether every consumer was read and every reading is the expected one.

    A consumer that could not be read, or has nothing to read it on, is not
    proof of anything: one matching consumer must not vouch for the rest.
    """

    read = {item["consumer"] for item in status["consumers"]}
    fingerprints = {item["fingerprint_sha256"] for item in status["consumers"]}
    return (
        not status["unreachable_consumers"]
        and all(consumer["name"] in read for consumer in spec["consumers"])
        and fingerprints == {expected_fingerprint}
    )


def _unserved(
    spec: dict[str, Any], status: dict[str, Any], expected_fingerprint: str
) -> dict[str, list[str]]:
    """Each consumer not shown serving the certificate, and what was found."""

    found: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for item in status["consumers"]:
        if item["fingerprint_sha256"] != expected_fingerprint:
            stale.setdefault(item["consumer"], []).append(item["domain"])
    for consumer, names in stale.items():
        found.setdefault(consumer, []).append(
            f"{consumer} still serves the previous certificate at " + ", ".join(sorted(names))
        )
    missed: dict[str, list[str]] = {}
    for item in status["unreachable_consumers"]:
        missed.setdefault(item["consumer"], [])
        if item["domain"]:
            missed[item["consumer"]].append(item["domain"])
    for consumer, names in missed.items():
        found.setdefault(consumer, []).append(
            f"{consumer} could not be read at " + ", ".join(sorted(names))
            if names
            else f"{consumer} could not be read"
        )
    read = {item["consumer"] for item in status["consumers"]}
    for consumer in spec["consumers"]:
        name = consumer["name"]
        if name not in read and name not in missed:
            found.setdefault(name, []).append(f"{name} has no verification domain")
    return found


def verify_tls_deployment(
    spec: dict[str, Any], expected_fingerprint: str
) -> ProviderResult:
    timeout, interval = _tls_verification_policy()
    deadline = time.monotonic() + timeout
    while True:
        result = reconcile_tls(spec)
        if tls_consumers_serve(spec, result.status, expected_fingerprint):
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
            unserved = _unserved(spec, result.status, expected_fingerprint)
            # Which consumer and which names, in the message itself.
            detail = "; ".join(
                line for _, lines in sorted(unserved.items()) for line in lines
            )
            raise ProviderError(
                f"{len(unserved)} of {len(spec['consumers'])} TLS consumers "
                f"did not activate the certificate within {timeout}s: {detail}.",
                status={
                    "expected_fingerprint_sha256": expected_fingerprint,
                    "consumers": evidence,
                },
            )
        time.sleep(interval)


def tls_match_evidence(
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
