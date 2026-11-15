"""The connections standard, and the readings HQ's standard judges the proxy,
the host firewall and the tailnet policy by.

The catalog emits the facts once. Each reading here is taken from what a
provider last reported, never from a probe, a vault or a guessed firewall
guarantee, and each is judged by the one ``standards.Check`` that states it
(``posture``), which the request standard and this page read alike.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.observations.host import FIREWALL_KIND
from hq.domains.control_plane.provider_adapters.contracts import IngressPolicy
from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND
from hq.domains.control_plane.providers import PROVIDERS, operator_label
from hq.platform.application.labels import dotted
from hq.platform.core.network import split_host_port
lazy from hq.domains.control_plane.models import ProviderInventory

from .connection_lifecycle import ATTENTION, WORKING
from .posture import SCOPE, STANDARD, Inputs, advanced
from .reach import TAILNET
from .request_standard import ADMISSION
from .standards import Check, CheckResult, Measurement, Reading, Standing, measure
from .tone import Tone
from .ui import counted, ended
lazy from . import tailnet
lazy from .facts import stored_snapshots
lazy from .moments import ago

# ----- The readings ------------------------------------------------------------------


def _tailnet_only(policy: IngressPolicy) -> bool:
    if policy.rules is None:
        return False
    tailnet_allows = tuple(("allow", str(network)) for network in TAILNET)
    deny_all = policy.rules == (*tailnet_allows, ("deny", "all")) or (
        policy.rules == tailnet_allows and policy.implicit_deny
    )
    return deny_all and not policy.satisfy_any and not policy.passes_auth and policy.authorizations == 0


def ingress_kinds() -> tuple[str, ...]:
    """The kinds whose observed records carry an ingress policy."""

    return tuple(kind for kind, spec in PROVIDERS.items() if spec.ingress_policy is not None)


def _edge_reading(hostname: str, snapshots: Mapping[str, Any]) -> Reading:
    """The first ingress policy any proxy reports for ``hostname``, judged."""

    host = normalized_hostname(split_host_port(hostname)[0])
    for kind in ingress_kinds():
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
            return _judged(policy, reachable=snapshot.reachable, proxy=operator_label(kind))
    return Reading.unavailable("Not read")


def _judged(policy: IngressPolicy, *, reachable: bool, proxy: str) -> Reading:
    if not reachable:
        return Reading.unavailable(
            "Last reading is out of date",
            detail=f"{proxy} is not answering, so this is from the last time HQ read it.",
        )
    if not policy.restricted:
        return Reading.unmet("No access list", detail=f"{proxy} has no access list on this name.")
    if policy.rules is None:
        return Reading.unavailable(
            "Rules not read yet", detail=f"{proxy} has an access list on this name, and HQ has not read its rules."
        )
    if not _tailnet_only(policy):
        return Reading.unmet(
            "Other sources allowed",
            detail=f"The {proxy} access list on this name allows more than the two tailnet address ranges.",
        )
    implicit = policy.implicit_deny and not any(directive == "deny" for directive, _address in policy.rules)
    return Reading.met(
        dotted("Tailnet ranges", f"{'implicit ' if implicit else ''}deny all"),
        detail=f"{proxy} allows tailnet IPv4 and IPv6 addresses for this name and refuses every other source.",
    )


def _allows_everyone(policy) -> bool:
    grants = any(
        "*" in (grant.get("src") or ()) and "*" in (grant.get("dst") or ()) and "*" in (grant.get("ip") or ())
        for grant in policy.grants
        if isinstance(grant, dict)
    )
    acls = any(
        rule.get("action", "accept") == "accept" and "*" in (rule.get("src") or ()) and "*:*" in (rule.get("dst") or ())
        for rule in policy.acls
    )
    return grants or acls


def tailnet_policy_reading() -> Reading:
    """Whether HQ read the policy active on the tailnet, and whether it lets
    every device reach every device."""

    if any(not snapshot.reachable for snapshot in tailnet.snapshots()[TAILNET_POLICY_KIND]):
        return Reading.unavailable(
            "Last reading is out of date",
            detail="Tailscale is not answering, so this is from the last time HQ read it.",
        )
    policy = tailnet.policy()
    if not policy.known and not policy.acls:
        return Reading.unavailable("Not read")
    if _allows_everyone(policy):
        return Reading.unmet("An allow-all rule", detail="A rule in the policy lets every device reach every device.")
    return Reading.met(
        dotted(counted(len(policy.grants) + len(policy.acls), "rule"), counted(len(policy.tests), "test")),
        detail="HQ read the policy that is active on the tailnet.",
    )


def _firewall_reading(snapshot) -> Reading:
    """Whether the firewall required the interface, not just the address.

    An address is a field the sender writes. This says whether the kernel also
    required the packet to arrive on the tailnet interface, which the sender
    cannot fake from somewhere else.
    """

    record = (
        next(
            (item for item in snapshot.records if isinstance(item, dict) and item.get("record") == "interface-binding"),
            None,
        )
        if snapshot is not None and snapshot.reachable
        else None
    )
    if record is None:
        return Reading.unavailable("Not read")
    interface = str(record.get("interface") or "the tailnet interface")
    if not record.get("accept_requires_interface"):
        return Reading.unmet(
            "Address only",
            detail="The firewall accepts this port by source address alone, so a tailnet address is accepted on any "
            "interface.",
        )
    if record.get("foreign_interface_dropped"):
        detail = (
            f"The firewall accepts this port only on {interface}, and drops a tailnet address that arrives on any "
            "other interface."
        )
    else:
        detail = (
            f"The firewall accepts this port only on {ended(interface)} It has no rule that drops a tailnet address "
            "arriving on another interface."
        )
    return Reading.met(f"Required on {interface}", detail=detail)


def request_readings(hostname: str) -> tuple[Reading, Reading]:
    """The proxy's and the host firewall's readings for a request, in one query.

    One query rather than one per kind: the panel's cost is asserted, and it is
    asserted because the failure it guards against is a page that reads an
    inventory once per device, per address, per layer or per header.
    """

    kinds = (*ingress_kinds(), FIREWALL_KIND)
    snapshots = {row.kind: row for row in ProviderInventory.objects.filter(kind__in=kinds)}
    return _edge_reading(hostname, snapshots), _firewall_reading(snapshots.get(FIREWALL_KIND))


def estate_readings(hostname: str) -> tuple[Reading, Reading]:
    """The same two readings, from the one read of every reading the estate's pages share."""

    stored = stored_snapshots()
    snapshots = {kind: stored[kind][0] for kind in (*ingress_kinds(), FIREWALL_KIND) if stored.get(kind)}
    return _edge_reading(hostname, snapshots), _firewall_reading(snapshots.get(FIREWALL_KIND))


# ----- The connections standard ------------------------------------------------------


def _connections(inputs: Inputs) -> tuple:
    return tuple(connection for group in inputs.groups for connection in group.connections)


def _custody(inputs: Inputs) -> Reading:
    """Where each connection's secret is kept, or that it needs none.

    Each connection says its credential model and one that holds a key names
    its store (``connection_catalog``), so every connection is one or the other.
    """

    connections = tuple((group, connection) for group in inputs.groups for connection in group.connections)
    if not connections:
        return Reading.unavailable("No connections yet")
    keyless = sum(connection.instance.keyless for _group, connection in connections)
    stores = Counter(group.spec.secret_store for group, connection in connections if not connection.instance.keyless)
    return Reading.met(
        dotted(
            *(f"{count} in {store}" for store, count in sorted(stores.items())),
            f"{keyless} need no key" if keyless else "",
        ),
        detail="HQ keeps no secret itself. Each connection names the place that keeps its secret.",
    )


def _freshness(inputs: Inputs) -> Reading:
    """Whether every connection has a reading, and how old the oldest is.

    A keyless public service is left out: it has no connection of its own to
    read, only the lookups made through it, which carry their own times.
    """

    connections = tuple(connection for connection in _connections(inputs) if not connection.instance.keyless)
    if not connections:
        return Reading.unavailable("Nothing read yet")
    read = tuple(connection for connection in connections if connection.instance.observed_at)
    oldest = min(read, key=lambda connection: connection.instance.observed_at, default=None)
    evidence = f"Oldest reading {ago(oldest.instance.observed_at)}" if oldest is not None else "Nothing read yet"
    if unread := len(connections) - len(read):
        return Reading.unmet(evidence, detail=f"{counted(unread, 'connection has', 'connections have')} no reading.")
    return Reading.met(evidence, detail="This page shows the last reading of each connection.")


# What each connection's credential is held to, beside the request's own checks.
CONNECTIONS_STANDARD: tuple[Check, ...] = (
    Check(
        "credentials",
        "Keys are kept in a vault",
        _custody,
        "a compromised web process leaking keys",
        "Name the store that keeps each connection's secret on its item.",
        unmet="A key is kept outside a vault",
        holds="HQ keeps no secret itself.",
    ),
    SCOPE,
    Check(
        "freshness",
        "Connections have been read",
        _freshness,
        "a page showing a connection HQ never read",
        "Start the controller, or press Refresh on the connection.",
        unmet="A connection has no reading",
        holds="Every connection has a reading.",
    ),
)


@dataclass(frozen=True, slots=True)
class ConnectionSecurityPosture:
    """The connections standard measured, beside how the request being
    answered was let in, where there is one."""

    checks: Measurement
    # HQ's standard, whose faults count against the posture too.
    standard: Measurement
    # The request standard's results that say how the request was let in.
    admission: tuple[CheckResult, ...]
    connection: Any
    connection_count: int
    healthy_count: int
    attention_count: int
    unverified_count: int
    ability_count: int
    external_custody_count: int
    dependency_count: int

    @property
    def results(self) -> tuple[CheckResult, ...]:
        """What the page answers, the request's first."""

        return (*self.admission, *self.checks.results)

    @property
    def network_gate_enforced(self) -> bool:
        return settings.SEVERINO_ENFORCE_TRUSTED_NETWORK

    @property
    def state(self) -> str:
        """Serious when a connection needs a person or a check counting against
        HQ is not met; neutral when something is unread or a pass is limited."""

        every = (*self.results, *self.standard.results)
        if self.attention_count or any(result.state == Standing.UNMET and result.enforced for result in every):
            return Tone.SERIOUS
        if self.unverified_count or any(result.tone != Tone.GOOD for result in self.results):
            return Tone.NEUTRAL
        return Tone.GOOD

    @property
    def admitted(self) -> bool:
        return bool(self.admission) and all(result.met for result in self.admission)

    @property
    def headline(self) -> str:
        if self.connection is None:
            return ""
        if self.admitted and self.connection.channel.id == "tailnet":
            return "You are connected over the tailnet."
        if self.admitted:
            return "You are connected over a private network."
        return "This connection needs a look."

    @property
    def summary(self) -> str:
        if self.connection is None:
            return "What each connection could reach the last time it was read."
        return "How this request reached HQ, and what each connection could reach the last time it was read."

    @property
    def trusted_proxy_count(self) -> int:
        if self.connection is None:
            return 0
        return self.connection.facts.forwarding.proxies

    @property
    def check_summary(self) -> str:
        """The checks counted by answer: "7 pass, 1 fail, 2 unknown"."""

        answers = Counter(result.answer for result in self.results)
        return ", ".join(
            f"{answers[answer]} {words}"
            for answer, words in (("Yes", "pass"), ("No", "fail"), ("Unknown", "unknown"), ("By design", "by design"))
            if answers[answer]
        )


def connection_security_posture(
    inputs: Inputs, standard: Measurement, *, connection: Any = None
) -> ConnectionSecurityPosture:
    """The posture from already-authorized, already-cached input: the estate
    alone, or with ``connection``, the request being answered, joined in."""

    connections = _connections(inputs)
    lifecycle = Counter(connection.lifecycle for connection in connections)
    # The headline counts the same lifecycle each row shows, so the two cannot
    # disagree about a connection.
    healthy = sum(lifecycle[state] for state in WORKING)
    attention = sum(lifecycle[state] for state in ATTENTION)
    return ConnectionSecurityPosture(
        checks=measure(inputs, CONNECTIONS_STANDARD, quiet=() if advanced() else STANDARD),
        standard=standard,
        admission=tuple(
            result
            for result in (connection.checks.results if connection is not None else ())
            if any(result.check is check for check in ADMISSION)
        ),
        connection=connection,
        connection_count=len(connections),
        healthy_count=healthy,
        attention_count=attention,
        unverified_count=len(connections) - healthy - attention,
        ability_count=sum(len(connection.abilities) for connection in connections),
        external_custody_count=sum(len(group.connections) for group in inputs.groups if group.spec.secret_store),
        dependency_count=sum(len(connection.instance.dependencies) for connection in connections),
    )
