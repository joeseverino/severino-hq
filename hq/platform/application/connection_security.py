"""One honest, query-free security posture for every connection surface.

The catalog emits the facts once. This module derives the explanation from
those facts and from the request decision HQ already made; it never probes a
provider, opens a vault, or invents an external firewall guarantee.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings

from hq.domains.control_plane.observations.host import FIREWALL_KIND
from hq.domains.control_plane.provider_adapters.contracts import IngressPolicy
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND
from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.platform.core.network import split_host_port

from .request_channel import channel_for_request
from .connection_catalog import ConnectionGroup
from .reach import TAILNET
from .ui import counted


# Each check is a statement that is true or not. The page answers it Yes, No,
# Check or Unknown from the state beside it.
EDGE_LABEL = "The proxy lets in tailnet addresses only"
FIREWALL_LABEL = "Arrived on the tailnet interface"
TAILNET_POLICY_LABEL = "HQ has read the tailnet policy"
# The word the page answers each state with.
ANSWERS = {
    "good": "Yes",
    "serious": "No",
    "bad": "No",
    "attention": "Check",
    "neutral": "Unknown",
}

# Lifecycle states that need a person: stale evidence, missing access, a
# failed probe, a rejected credential.
ATTENTION_LIFECYCLES = ("stale", "unauthorized", "unreachable", "revoked")

@dataclass(frozen=True)
class SecurityControl:
    """One independently checkable part of the connection boundary."""

    id: str
    label: str
    state: str
    evidence: str
    detail: str

    @property
    def answer(self) -> str:
        return ANSWERS.get(self.state, ANSWERS["neutral"])


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
    # None when no request is being answered.
    secure_transport: bool | None
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

    @property
    def check_summary(self) -> str:
        """The checks counted by answer: "7 pass, 1 to look at, 2 unknown"."""

        answers = Counter(control.answer for control in self.controls)
        return ", ".join(
            f"{answers[answer]} {words}"
            for answer, words in (
                ("Yes", "pass"),
                ("No", "fail"),
                ("Check", "to look at"),
                ("Unknown", "unknown"),
            )
            if answers[answer]
        )


def _unattested_edge() -> SecurityControl:
    return SecurityControl(
        "edge",
        EDGE_LABEL,
        "neutral",
        "Not read",
        "HQ has no reading of an access list for this name.",
    )


def _unattested_firewall() -> SecurityControl:
    return SecurityControl(
        "host-firewall",
        FIREWALL_LABEL,
        "neutral",
        "Not read",
        "HQ has no firewall reading for this machine, so it cannot say which "
        "interface this request had to arrive on.",
    )


def _unattested_tailnet_policy() -> SecurityControl:
    return SecurityControl(
        "tailnet-policy",
        TAILNET_POLICY_LABEL,
        "neutral",
        "Not read",
        "HQ has no reading of the tailnet policy.",
    )


def _tailnet_only(policy: IngressPolicy) -> bool:
    if policy.rules is None:
        return False
    tailnet_allows = tuple(("allow", str(network)) for network in TAILNET)
    deny_all = policy.rules == (*tailnet_allows, ("deny", "all")) or (
        policy.rules == tailnet_allows and policy.implicit_deny
    )
    return (
        deny_all
        and not policy.satisfy_any
        and not policy.passes_auth
        and policy.authorizations == 0
    )


def _ingress_kinds() -> tuple[str, ...]:
    """The kinds whose observed records carry an ingress policy."""

    return tuple(kind for kind, spec in PROVIDERS.items() if spec.ingress_policy is not None)


def _proxy_label(kind: str) -> str:
    spec = PROVIDERS[kind]
    return next(
        (CONNECTION_LABELS[name] for name in spec.connection_providers if name in CONNECTION_LABELS),
        spec.label,
    )


def _ingress_control(hostname: str, snapshots: Mapping[str, Any]) -> SecurityControl:
    """The first ingress policy any proxy reports for ``hostname``, judged."""

    host = normalized_hostname(split_host_port(hostname)[0])
    for kind in _ingress_kinds():
        snapshot = snapshots.get(kind)
        if snapshot is None or not host:
            continue
        read = PROVIDERS[kind].ingress_policy
        policy = next(
            (
                found
                for found in (read(item) for item in snapshot.records if isinstance(item, dict))
                if host in found.hostnames
            ),
            None,
        )
        if policy is not None:
            return _judged(policy, reachable=snapshot.reachable, proxy=_proxy_label(kind))
    return _unattested_edge()


def _judged(policy: IngressPolicy, *, reachable: bool, proxy: str) -> SecurityControl:
    if not reachable:
        return SecurityControl(
            "edge",
            EDGE_LABEL,
            "neutral",
            "Last reading is out of date",
            f"{proxy} is not answering, so this is from the last time HQ read it.",
        )
    if not policy.restricted:
        return SecurityControl(
            "edge",
            EDGE_LABEL,
            "serious",
            "No access list",
            f"{proxy} has no access list on this name.",
        )
    if policy.rules is None:
        return SecurityControl(
            "edge",
            EDGE_LABEL,
            "neutral",
            "Rules not read yet",
            f"{proxy} has an access list on this name, and HQ has not read its rules.",
        )
    if not _tailnet_only(policy):
        return SecurityControl(
            "edge",
            EDGE_LABEL,
            "serious",
            "Other sources allowed",
            f"The {proxy} access list on this name allows more than the two "
            "tailnet address ranges.",
        )
    implicit = policy.implicit_deny and not any(
        directive == "deny" for directive, _address in policy.rules
    )
    return SecurityControl(
        "edge",
        EDGE_LABEL,
        "good",
        f"Tailnet ranges · {'implicit ' if implicit else ''}deny all",
        f"{proxy} allows tailnet IPv4 and IPv6 addresses for this name and "
        "refuses every other source.",
    )


def _tailnet_policy_control(snapshot) -> SecurityControl:
    if snapshot is None:
        return _unattested_tailnet_policy()
    if not snapshot.reachable:
        return SecurityControl(
            "tailnet-policy",
            TAILNET_POLICY_LABEL,
            "neutral",
            "Last reading is out of date",
            "Tailscale is not answering, so this is from the last time HQ read it.",
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
        TAILNET_POLICY_LABEL,
        "good",
        f"{counted(len(grants), 'grant')} · {counted(len(tests), 'test')}",
        "HQ read the policy that is active on the tailnet.",
    )


def _host_firewall_control(snapshot) -> SecurityControl:
    """Whether the firewall required the interface, not just the address.

    The line above this one on the page says the address is on the tailnet, and
    an address is a field the sender writes. This says whether the kernel also
    required the packet to arrive on the tailnet interface, which the sender
    cannot fake from somewhere else.
    """

    if snapshot is None or not snapshot.reachable:
        return _unattested_firewall()
    record = next(
        (
            item
            for item in snapshot.records
            if isinstance(item, dict) and item.get("record") == "interface-binding"
        ),
        None,
    )
    if record is None:
        return _unattested_firewall()

    interface = str(record.get("interface") or "the tailnet interface")
    accepts = bool(record.get("accept_requires_interface"))
    drops = bool(record.get("foreign_interface_dropped"))
    if accepts and drops:
        return SecurityControl(
            "host-firewall",
            FIREWALL_LABEL,
            "good",
            f"Required on {interface}",
            f"The firewall accepts this port only on {interface}, and drops a "
            "tailnet address that arrives on any other interface.",
        )
    if accepts:
        return SecurityControl(
            "host-firewall",
            FIREWALL_LABEL,
            "good",
            f"Required on {interface}",
            f"The firewall accepts this port only on {interface}. It has no rule "
            "that drops a tailnet address arriving on another interface.",
        )
    return SecurityControl(
        "host-firewall",
        FIREWALL_LABEL,
        "bad",
        "Address only",
        "The firewall accepts this port by source address alone, so a tailnet "
        "address is accepted on any interface.",
    )


def _snapshots(*kinds: str) -> dict:
    """The stored inventory of each kind, by kind, in one query."""

    from hq.domains.control_plane.models import ProviderInventory

    return {row.kind: row for row in ProviderInventory.objects.filter(kind__in=kinds)}


def observed_request_controls(hostname: str) -> tuple[SecurityControl, SecurityControl]:
    """The two cached readings the request panel projects, in one query.

    One query rather than one per kind: the panel's cost is asserted, and it is
    asserted because the failure it guards against is a page that reads an
    inventory once per device, per address, per layer or per header.
    """

    snapshots = _snapshots(*_ingress_kinds(), FIREWALL_KIND)
    return (
        _ingress_control(hostname, snapshots),
        _host_firewall_control(snapshots.get(FIREWALL_KIND)),
    )


def observed_connection_controls(
    hostname: str,
) -> tuple[SecurityControl, SecurityControl]:
    """Read both provider controls in one constant local-cache query."""

    snapshots = _snapshots(*_ingress_kinds(), TAILNET_POLICY_KIND)
    return (
        _tailnet_policy_control(snapshots.get(TAILNET_POLICY_KIND)),
        _ingress_control(hostname, snapshots),
    )


@dataclass(frozen=True)
class _Admission:
    """How the request being answered reached HQ: the three controls it decides."""

    network: SecurityControl
    transport: SecurityControl
    proxy: SecurityControl
    holds: bool
    channel_id: str
    channel_label: str
    trusted_proxies: int
    secure: bool


# Controls that describe the request being answered rather than the estate.
REQUEST_CONTROLS = frozenset({"network", "transport", "proxy"})


def _admission(request, gate: bool) -> _Admission:
    channel = channel_for_request(request)
    secure = bool(request.is_secure())
    from .request_path import address_chain

    trusted_proxies = sum(item.role == "proxy" for item in address_chain(request))
    forwarded = bool(request.META.get("HTTP_X_FORWARDED_FOR", ""))
    return _Admission(
        network=SecurityControl(
            "network",
            "Only your networks can open HQ",
            "good" if gate and channel.private else "serious",
            f"{channel.label} · {'enforced' if gate else 'not enforced'}",
            "HQ refuses any address outside its private ranges."
            if gate
            else "HQ is not refusing addresses outside its private ranges.",
        ),
        transport=SecurityControl(
            "transport",
            "This request was encrypted",
            "good" if secure else "attention",
            ("TLS inside WireGuard" if channel.id == "tailnet" else "TLS")
            if secure
            else "Plain HTTP",
            "This request arrived over TLS."
            if secure
            else "This request did not arrive over TLS.",
        ),
        proxy=SecurityControl(
            "proxy",
            "Your address comes from a proxy HQ trusts",
            "good" if trusted_proxies else "neutral",
            (
                counted(trusted_proxies, "trusted proxy", "trusted proxies")
                if trusted_proxies
                else "Forwarded address ignored"
                if forwarded
                else "No proxy"
            ),
            "A proxy HQ trusts passed your address on."
            if trusted_proxies
            else "A proxy HQ does not trust sent an address, and HQ ignored it."
            if forwarded
            else "This request came straight to HQ.",
        ),
        holds=gate and channel.private and secure,
        channel_id=channel.id,
        channel_label=channel.label,
        trusted_proxies=trusted_proxies,
        secure=secure,
    )


def _headline(admission: _Admission | None) -> str:
    if admission is None:
        return ""
    if admission.holds and admission.channel_id == "tailnet":
        return "You are connected over the tailnet."
    if admission.holds:
        return "You are connected over a private network."
    return "This connection needs a look."


def _custody_control(groups: tuple[ConnectionGroup, ...]) -> SecurityControl:
    """Whether every connection either names where its secret is kept or needs none."""

    stores = Counter(
        group.spec.secret_store
        for group in groups
        for _connection in group.connections
        if group.spec.secret_store
    )
    unkept = tuple(
        connection
        for group in groups
        if not group.spec.secret_store
        for connection in group.connections
    )
    keyless = sum(connection.instance.credential_model == "none" for connection in unkept)
    total = sum(stores.values()) + len(unkept)
    unknown = len(unkept) - keyless
    return SecurityControl(
        "credentials",
        "HQ knows where every secret is kept",
        "good" if total and not unknown else "neutral",
        " · ".join(
            part
            for part in (
                *(f"{count} in {store}" for store, count in sorted(stores.items())),
                f"{keyless} need no secret" if keyless else "",
                f"{unknown} do not say" if unknown else "",
            )
            if part
        )
        or "No connections yet",
        "HQ keeps no secret itself. Each connection names the place that keeps its secret."
        if total
        else "No connection has been read yet.",
    )


def _scope_control(evidence: Counter, any_abilities: bool) -> SecurityControl:
    """Whether each thing HQ does through a credential is within checked permissions."""

    unchecked = evidence["undeclared"] + evidence["unverified"]
    unknown = evidence["unknown"]
    missing = evidence["missing"] + evidence["revoked"]
    coarse = evidence["coarse"]
    if missing:
        state, detail = "serious", "A credential lacks a permission HQ needs."
    elif unchecked or unknown:
        state, detail = (
            "attention",
            "HQ cannot confirm these credentials are limited to what it needs.",
        )
    elif coarse:
        state, detail = (
            "neutral",
            "These services offer nothing narrower than full account access.",
        )
    else:
        state, detail = "good", "Each service confirmed the permissions HQ needs."
    return SecurityControl(
        "scope",
        "Every credential is limited to what HQ needs",
        state,
        " · ".join(
            f"{count} {words}"
            for count, words in (
                (evidence["verified"], "checked"),
                (coarse, "with full account access"),
                (evidence["not_applicable"], "need no key"),
                (unchecked, "not checked"),
                (unknown, "not reported"),
                (missing, "missing"),
            )
            if count
        )
        or "Nothing to check",
        detail if any_abilities else "No connection reads or changes anything yet.",
    )


def _freshness_control(connections: tuple) -> SecurityControl:
    """Whether every connection has a reading, and how old the oldest is."""

    from .moments import ago

    read = tuple(
        connection for connection in connections if connection.instance.observed_at
    )
    oldest = min(
        read, key=lambda connection: connection.instance.observed_at, default=None
    )
    unread = len(connections) - len(read)
    return SecurityControl(
        "freshness",
        "Every connection has been read",
        "good" if connections and not unread else "neutral",
        (
            f"Oldest reading {ago(oldest.instance.observed_at)}"
            if oldest is not None
            else "Nothing read yet"
        ),
        f"{counted(unread, 'connection has', 'connections have')} no reading."
        if unread
        else "This page shows the last reading of each connection.",
    )


def connection_security_posture(
    groups: tuple[ConnectionGroup, ...],
    *,
    request=None,
    tailnet_policy: SecurityControl | None = None,
    edge: SecurityControl | None = None,
) -> ConnectionSecurityPosture:
    """Derive security posture from already-authorized, already-cached input.

    Without ``request`` it is the estate alone: the controls in
    ``REQUEST_CONTROLS`` are absent and the headline is blank.
    """

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
    lifecycle = Counter(connection.lifecycle for connection in connections)
    # The headline counts the same lifecycle each row shows, so the two cannot
    # disagree about a connection.
    healthy = lifecycle["ready"] + lifecycle["reachable"]
    attention = sum(lifecycle[state] for state in ATTENTION_LIFECYCLES)
    unverified = len(connections) - healthy - attention
    evidence = Counter(state.evidence for state in states)
    scope_verified = evidence["verified"]
    scope_coarse = evidence["coarse"]
    scope_keyless = evidence["not_applicable"]
    scope_undeclared = evidence["undeclared"] + evidence["unverified"]
    scope_missing = evidence["missing"] + evidence["revoked"]
    scope_unknown = evidence["unknown"]
    external_custody = sum(
        len(group.connections) for group in groups if group.spec.secret_store
    )
    dependencies = sum(
        len(connection.instance.dependencies) for connection in connections
    )

    gate = bool(getattr(settings, "SEVERINO_ENFORCE_TRUSTED_NETWORK", False))
    admission = _admission(request, gate) if request is not None else None
    tailnet_policy_control = tailnet_policy or _unattested_tailnet_policy()
    edge_control = edge or _unattested_edge()

    controls = (
        *((admission.network,) if admission else ()),
        tailnet_policy_control,
        *((admission.transport, admission.proxy) if admission else ()),
        _custody_control(groups),
        _scope_control(evidence, bool(states)),
        _freshness_control(connections),
        edge_control,
    )

    state = (
        "serious"
        if (admission is not None and not admission.holds)
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
        headline=_headline(admission),
        summary=(
            "How this request reached HQ, and what each connection could reach "
            "the last time it was read."
            if admission
            else "What each connection could reach the last time it was read."
        ),
        controls=tuple(controls),
        channel_label=admission.channel_label if admission else "",
        trusted_proxy_count=admission.trusted_proxies if admission else 0,
        network_gate_enforced=gate,
        secure_transport=admission.secure if admission else None,
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
