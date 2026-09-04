"""One honest, query-free security posture for every connection surface.

The catalog emits the facts once. This module derives the explanation from
those facts and from the request decision HQ already made; it never probes a
provider, opens a vault, or invents an external firewall guarantee.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings

from .connection import channel_for_request, hops_of
from .connections import ConnectionGroup
from .reach import TAILNET


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
    scope_coarse_count: int
    scope_keyless_count: int
    scope_undeclared_count: int
    scope_missing_count: int
    scope_unknown_count: int
    ready_count: int
    stale_count: int
    revoked_count: int
    external_custody_count: int
    dependency_count: int


def _unattested_edge() -> SecurityControl:
    return SecurityControl(
        "edge",
        "External edge",
        "neutral",
        "Not attested here",
        "HQ has not received a provider observation that proves a source policy "
        "for this hostname.",
    )


def _unattested_tailnet_policy() -> SecurityControl:
    return SecurityControl(
        "tailnet-policy",
        "Tailscale policy",
        "neutral",
        "Not observed",
        "No current Tailscale policy observation is available.",
    )


def _tailnet_only(policy: dict[str, Any]) -> bool:
    clients = policy.get("clients")
    if not isinstance(clients, list):
        return False
    rules = [
        (str(rule.get("directive", "")).lower(), str(rule.get("address", "")).lower())
        for rule in clients
        if isinstance(rule, dict)
    ]
    tailnet_allows = [("allow", str(network)) for network in TAILNET]
    deny_all = rules == [*tailnet_allows, ("deny", "all")] or (
        rules == tailnet_allows and policy.get("implicit_deny") is True
    )
    return (
        deny_all
        and policy.get("satisfy_any") is False
        and policy.get("pass_auth") is False
        and policy.get("authorization_count") == 0
    )


def _ingress_control(hostname: str, snapshot) -> SecurityControl:
    host = hostname.partition(":")[0].strip().lower().rstrip(".")
    if snapshot is None:
        return _unattested_edge()
    record = next(
        (
            item
            for item in snapshot.records
            if isinstance(item, dict)
            and host
            in {
                str(name).strip().lower().rstrip(".")
                for name in item.get("domain_names") or ()
            }
        ),
        None,
    )
    if record is None:
        return _unattested_edge()
    if not snapshot.reachable:
        return SecurityControl(
            "edge",
            "Ingress policy",
            "neutral",
            "Last proof is aging",
            "The NPM connection is not currently reachable. HQ keeps the last "
            "observation visible without presenting it as current proof.",
        )
    if not record.get("access_list_id"):
        return SecurityControl(
            "edge",
            "Ingress policy",
            "serious",
            "No source restriction",
            "NPM reports no access list on this hostname.",
        )
    policy = record.get("access_policy")
    if not isinstance(policy, dict):
        return SecurityControl(
            "edge",
            "Ingress policy",
            "neutral",
            "Rules not yet observed",
            "NPM reports an assigned policy, but the cached sweep predates "
            "rule-level evidence.",
        )
    if not _tailnet_only(policy):
        return SecurityControl(
            "edge",
            "Ingress policy",
            "serious",
            "Not Tailnet-only",
            "The assigned NPM policy does not exactly allow both Tailscale "
            "address ranges and then deny every other source without proxy auth.",
        )
    implicit = policy.get("implicit_deny") is True and not any(
        str(rule.get("directive", "")).lower() == "deny"
        for rule in policy.get("clients") or ()
        if isinstance(rule, dict)
    )
    return SecurityControl(
        "edge",
        "Ingress policy",
        "good",
        f"Tailnet ranges · {'implicit ' if implicit else ''}deny all",
        "NPM's authenticated API reports that this hostname allows Tailscale "
        "IPv4 and IPv6 sources, denies everything else"
        f"{' through its generated final rule' if implicit else ''}, and passes "
        "no proxy credentials to HQ.",
    )


def _tailnet_policy_control(snapshot) -> SecurityControl:
    if snapshot is None:
        return _unattested_tailnet_policy()
    if not snapshot.reachable:
        return SecurityControl(
            "tailnet-policy",
            "Tailscale policy",
            "neutral",
            "Last proof is aging",
            "The Tailscale policy connection is not currently reachable. HQ "
            "keeps the prior observation visible without calling it current.",
        )
    record = next(
        (
            item
            for item in snapshot.records
            if isinstance(item, dict) and item.get("record") == "policy"
        ),
        None,
    )
    if record is None:
        return _unattested_tailnet_policy()
    grants = record.get("grants") if isinstance(record.get("grants"), list) else []
    tests = record.get("tests") if isinstance(record.get("tests"), list) else []
    return SecurityControl(
        "tailnet-policy",
        "Tailscale policy",
        "good",
        (
            f"Observed · {len(grants)} grant{'s' if len(grants) != 1 else ''} · "
            f"{len(tests)} test{'s' if len(tests) != 1 else ''}"
        ),
        "HQ read the active policy through its scoped Tailscale connection. "
        "The request inspector applies its device-to-HQ verdict to this request.",
    )


def observed_ingress_control(hostname: str) -> SecurityControl:
    """Derive one hostname's edge control from the cached NPM observation."""

    from control_plane.models import ProviderInventory

    return _ingress_control(
        hostname, ProviderInventory.objects.filter(kind="npm.proxy_host").first()
    )


def observed_connection_controls(
    hostname: str,
) -> tuple[SecurityControl, SecurityControl]:
    """Read both provider controls in one constant local-cache query."""

    from control_plane.models import ProviderInventory

    snapshots = {
        row.kind: row
        for row in ProviderInventory.objects.filter(
            kind__in=("npm.proxy_host", "tailscale.policy")
        )
    }
    return (
        _tailnet_policy_control(snapshots.get("tailscale.policy")),
        _ingress_control(hostname, snapshots.get("npm.proxy_host")),
    )


def connection_security_posture(
    groups: tuple[ConnectionGroup, ...],
    *,
    request,
    tailnet_policy: SecurityControl | None = None,
    edge: SecurityControl | None = None,
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
    evidence = Counter(state.evidence for state in states)
    scope_verified = evidence["verified"]
    scope_coarse = evidence["coarse"]
    scope_keyless = evidence["not_applicable"]
    scope_undeclared = evidence["undeclared"] + evidence["unverified"]
    scope_missing = evidence["missing"] + evidence["revoked"]
    scope_unknown = evidence["unknown"]
    lifecycle = Counter(connection.lifecycle for connection in connections)
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
    tailnet_policy_control = tailnet_policy or _unattested_tailnet_policy()
    edge_control = edge or _unattested_edge()

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
        tailnet_policy_control,
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
            "good"
            if connections and external_custody == len(connections)
            else "neutral",
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
            "serious"
            if scope_missing
            else "attention"
            if scope_undeclared or scope_unknown
            else "neutral"
            if scope_coarse
            else "good",
            " · ".join(
                part
                for part in (
                    f"{scope_verified} verified",
                    f"{scope_coarse} whole-account",
                    f"{scope_keyless} keyless",
                    f"{scope_undeclared} undeclared" if scope_undeclared else "",
                    f"{scope_unknown} unknown" if scope_unknown else "",
                    f"{scope_missing} missing" if scope_missing else "",
                )
                if part
            ),
            "Verified means the provider reported the grants the ability needs. "
            "Whole-account means the provider's credential model offers nothing "
            "narrower, so the evidence is the credential kind itself. Keyless "
            "abilities have no grant to prove. Undeclared and unknown evidence "
            "never become permission, and a missing grant fails closed."
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
        edge_control,
    )

    if ingress_holds and channel.id == "tailnet":
        headline = "Tailnet ingress. Explicit authority."
    elif ingress_holds:
        headline = "Private ingress. Explicit authority."
    else:
        headline = "Ingress needs attention. Authority stays explicit."
    state = (
        "serious"
        if not ingress_holds
        or attention
        or scope_missing
        or tailnet_policy_control.state == "serious"
        or edge_control.state == "serious"
        else "neutral"
        if unverified or scope_unknown or scope_undeclared or scope_coarse
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
        scope_coarse_count=scope_coarse,
        scope_keyless_count=scope_keyless,
        scope_undeclared_count=scope_undeclared,
        scope_missing_count=scope_missing,
        scope_unknown_count=scope_unknown,
        ready_count=lifecycle["ready"],
        stale_count=lifecycle["stale"],
        revoked_count=lifecycle["revoked"],
        external_custody_count=external_custody,
        dependency_count=dependencies,
    )
