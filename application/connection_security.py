"""One honest, query-free security posture for every connection surface.

The catalog emits the facts once. This module derives the explanation from
those facts and from the request decision HQ already made; it never probes a
provider, opens a vault, or invents an external firewall guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings

from .connection import channel_for_request, hops_of
from .connections import ConnectionGroup


@dataclass(frozen=True)
class SecurityControl:
    """One independently checkable part of the connection boundary."""

    id: str
    label: str
    state: str
    evidence: str
    detail: str


@dataclass(frozen=True)
class ConnectionSecurityPosture:
    """The current request and cached connection estate, without secret data."""

    state: str
    headline: str
    summary: str
    controls: tuple[SecurityControl, ...]
    channel_label: str
    trusted_proxy_count: int
    network_gate_enforced: bool
    secure_transport: bool
    connection_count: int
    healthy_count: int
    attention_count: int
    unverified_count: int
    observed_count: int
    oldest_observed_at: datetime | None
    ability_count: int
    scope_verified_count: int
    scope_missing_count: int
    scope_unknown_count: int
    external_custody_count: int
    dependency_count: int


def connection_security_posture(
    groups: tuple[ConnectionGroup, ...], *, request
) -> ConnectionSecurityPosture:
    """Derive security posture from already-authorized, already-cached input."""

    connections = tuple(
        connection for group in groups for connection in group.connections
    )
    states = tuple(
        state for connection in connections for state in connection.abilities
    )
    observed = tuple(
        connection.instance.observed_at
        for connection in connections
        if connection.instance.observed_at is not None
    )
    healthy = sum(connection.instance.status == "good" for connection in connections)
    attention = sum(
        connection.instance.status in {"attention", "serious"}
        for connection in connections
    )
    unverified = len(connections) - healthy - attention
    scope_verified = sum(state.available is True for state in states)
    scope_missing = sum(state.available is False for state in states)
    scope_unknown = sum(state.available is None for state in states)
    external_custody = sum(
        len(group.connections) for group in groups if group.spec.secret_store
    )
    dependencies = sum(
        len(connection.instance.dependencies) for connection in connections
    )

    channel = channel_for_request(request)
    gate = bool(getattr(settings, "SEVERINO_ENFORCE_TRUSTED_NETWORK", False))
    secure = bool(request.is_secure())
    hops = hops_of(request)
    trusted_proxies = sum(hop.role == "proxy" for hop in hops)
    forwarded = bool(request.META.get("HTTP_X_FORWARDED_FOR", ""))
    ingress_holds = gate and channel.private and secure

    controls = (
        SecurityControl(
            "network",
            "Network admission",
            "good" if gate and channel.private else "serious",
            f"{channel.label} · {'enforced' if gate else 'not enforced'}",
            "HQ refuses addresses outside its private ranges before sessions, "
            "authentication, static assets, or views run."
            if gate
            else "This deployment is not enforcing HQ's trusted-network gate.",
        ),
        SecurityControl(
            "transport",
            "Transport",
            "good" if secure else "attention",
            "TLS" if secure else "Plain HTTP",
            "This request arrived over TLS. Tailnet traffic has its own "
            "WireGuard layer when the caller channel is Tailnet."
            if secure
            else "This request did not arrive over TLS.",
        ),
        SecurityControl(
            "proxy",
            "Proxy identity",
            "good" if trusted_proxies else "neutral",
            (
                f"{trusted_proxies} trusted proxy "
                f"hop{'s' if trusted_proxies != 1 else ''}"
                if trusted_proxies
                else "Forwarded identity ignored"
                if forwarded
                else "Direct request"
            ),
            "HQ walks the forwarded chain from the trusted peer inward and "
            "judges the first address it can prove."
            if trusted_proxies
            else "No trusted proxy supplied the caller identity for this request."
            if forwarded
            else "No proxy assertion was needed for this request.",
        ),
        SecurityControl(
            "credentials",
            "Credential custody",
            "good" if connections and external_custody == len(connections) else "neutral",
            f"{external_custody} of {len(connections)} externally custodied",
            "Credential values have no field in the connection contract. "
            "Families emit identifiers and cached observations and name the "
            "system that keeps their secrets."
            if connections
            else "No configured connection has reported credential custody yet.",
        ),
        SecurityControl(
            "authorization",
            "Capability authorization",
            "good",
            f"{len(groups)} permitted connection type{'s' if len(groups) != 1 else ''}",
            "HQ checks every family's required capabilities before invoking "
            "its instance provider, so an unauthorized reader cannot trigger it.",
        ),
        SecurityControl(
            "scope",
            "Least-privilege evidence",
            "attention" if scope_missing else "neutral" if scope_unknown else "good",
            (
                f"{scope_verified} verified · {scope_missing} missing · "
                f"{scope_unknown} unknown"
            ),
            "Each ability is checked against the scopes its connection reported. "
            "Unknown stays unknown; it is never presented as permission."
            if states
            else "No connection abilities have been declared yet.",
        ),
        SecurityControl(
            "metadata",
            "Safe metadata contract",
            "good",
            "Validated before display",
            "Unknown abilities, unsafe relationship links, duplicate identities, "
            "and endpoint userinfo, queries, or fragments fail closed.",
        ),
        SecurityControl(
            "freshness",
            "Cached evidence",
            "good" if connections and len(observed) == len(connections) else "neutral",
            f"{len(observed)} of {len(connections)} timestamped",
            "This page derives its answer from stored observations; opening it "
            "does not probe providers or open a secret store.",
        ),
        SecurityControl(
            "edge",
            "External edge",
            "neutral",
            "Not attested here",
            "HQ cannot infer router, firewall, or public port-forwarding state "
            "from an application request. The request-path inspector proves "
            "what HQ can see without pretending to prove the rest.",
        ),
    )

    if ingress_holds and channel.id == "tailnet":
        headline = "Tailnet ingress. Explicit authority."
    elif ingress_holds:
        headline = "Private ingress. Explicit authority."
    else:
        headline = "Ingress needs attention. Authority stays explicit."
    state = (
        "serious"
        if not ingress_holds or attention or scope_missing
        else "neutral"
        if unverified or scope_unknown
        else "good"
    )
    return ConnectionSecurityPosture(
        state=state,
        headline=headline,
        summary=(
            "Current request admission joined to every connection's cached "
            "reach, abilities, scopes, and dependents. This page triggers no probe."
        ),
        controls=controls,
        channel_label=channel.label,
        trusted_proxy_count=trusted_proxies,
        network_gate_enforced=gate,
        secure_transport=secure,
        connection_count=len(connections),
        healthy_count=healthy,
        attention_count=attention,
        unverified_count=unverified,
        observed_count=len(observed),
        oldest_observed_at=min(observed, default=None),
        ability_count=len(states),
        scope_verified_count=scope_verified,
        scope_missing_count=scope_missing,
        scope_unknown_count=scope_unknown,
        external_custody_count=external_custody,
        dependency_count=dependencies,
    )
