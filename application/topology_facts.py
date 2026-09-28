"""What the connections themselves report, attached to the nodes they read: the perimeter, the tailnet, and the policy each observation is judged against."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from control_plane.names import normalized_hostname
from control_plane.provider_adapters.tailscale import TAILNET_KIND
from control_plane.providers import PROVIDERS

from .topology_model import TopologyNode


def _inventory_of(kind: str) -> tuple[Any, ...]:
    """Snapshots of one kind, from the join engine's one read of the inventory."""

    from .facts import snapshots_of

    return snapshots_of(kind)


def perimeter_unchecked(record: dict[str, Any]) -> str:
    """Why a perimeter reading proves nothing, or "" when it tried something.

    No address or no port means no connection was attempted, so "nothing
    answered" is not evidence of a shut perimeter.
    """

    if not record.get("public_addresses"):
        return "no public address"
    if not record.get("ports_checked"):
        return "no port to try"
    return ""


def _perimeter_facts() -> dict[str, tuple[tuple[str, str], ...]]:
    """Each machine's perimeter reading, keyed by the connection that took it."""

    found: dict[str, tuple[tuple[str, str], ...]] = {}
    from control_plane.observations.host import PERIMETER_KIND

    for snapshot in _inventory_of(PERIMETER_KIND):
        for record in snapshot.records:
            connection_ref = str(record.get("connection_ref", "")).strip()
            if not connection_ref:
                continue
            entries: list[tuple[str, str]] = []
            unchecked = perimeter_unchecked(record)
            if unchecked:
                entries.append(("perimeter-unchecked", unchecked))
            unit = str(record.get("firewall_unit", "")).strip()
            if unit and unit != "active":
                entries.append(("firewall-unit", unit))
            entries.extend(
                ("answers-publicly", str(port))
                for port in record.get("answered_publicly") or ()
            )
            if entries:
                found[connection_ref] = tuple(entries)
    return found


_EXIT_ROUTES = frozenset({"0.0.0.0/0", "::/0"})


def _tailnet_facts() -> tuple[tuple[str, str], ...]:
    """What the tailnet uses, and each global resolver that is not part of it.

    Addresses are the IPv4 addresses of every device the device reading holds;
    routes are the subnet routes approved for them, exit routes excluded.
    Nothing is said without a device reading, since every claim here compares
    against it.
    """

    from core.network import parse_ip

    addresses: set[str] = set()
    routes: set[str] = set()
    for snapshot in _inventory_of(TAILNET_KIND):
        for record in snapshot.records:
            addresses.update(str(item) for item in record.get("addresses") or ())
            routes.update(
                str(route)
                for route in record.get("enabled_routes") or ()
                if str(route) not in _EXIT_ROUTES
            )
    if not addresses:
        return ()
    entries: list[tuple[str, str]] = []
    for snapshot in _inventory_of("tailscale.dns"):
        for record in snapshot.records:
            entries.extend(
                ("tailnet-dns-off-tailnet", str(address))
                for address in record.get("nameservers") or ()
                if parse_ip(str(address)) is not None and str(address) not in addresses
            )
    entries.extend(
        ("tailnet-address", address)
        for address in sorted(addresses)
        if parse_ip(address) is not None
    )
    entries.extend(("tailnet-route", route) for route in sorted(routes))
    return tuple(entries)


def _policy_verdicts(
    found: dict[str, tuple[tuple[str, str], ...]],
    blocked: list[tuple[str, dict[str, str]]],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Add, for each unreachable address, whether the tailnet is what refused.

    Three answers are possible and only one of them is this fact. A policy that
    admits the path leaves nothing here: the consumer is down, or the service
    is not listening, and saying "the tailnet allows this" would be noise. A
    tailnet HQ has not swept leaves nothing either: not knowing is not the same
    as knowing it is shut, and a rule that confused them would send an operator
    to change an access policy that was never the problem.
    """

    from .tailnet import devices, device_at, may_reach, observer

    known = devices()
    watcher = observer(known)
    if watcher is None:
        return found
    for node_id, item in blocked:
        target = device_at(str(item.get("endpoint", "")), known)
        if target is None:
            continue
        try:
            port = int(str(item.get("port", "")) or 0)
        except ValueError:
            continue
        if not port:
            continue
        verdict = may_reach(watcher.name, target.name, port, known)
        if verdict.allowed or not verdict.known:
            continue
        found[node_id] = found.get(node_id, ()) + (
            ("path-denied", f"{watcher.name} to {target.name} on {port}"),
        )
    return found


def add_observed_facts(
    resources: tuple[Any, ...],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """The observed facts a rule needs, keyed by the node they belong to.

    A domain's registration, which lives in the zone sweep rather than in any
    declaration (nobody writes down when a domain expires, the registrar is
    asked) and the consumers a reading could not reach, which a sweep records
    and no declaration mentions. A rule reasoning about either has no other way
    to see it, and rules may not query.
    """


    from .zones import ZONE_KIND

    found: dict[str, tuple[tuple[str, str], ...]] = {}

    # Already in hand, so this costs nothing: the sweep wrote it into the
    # status this function was handed.
    blocked: list[tuple[str, dict[str, str]]] = []
    for resource in resources:
        unreachable = (resource.status or {}).get("unreachable_consumers") or []
        if not isinstance(unreachable, list):
            continue
        entries = tuple(
            (
                "unreachable",
                str(item.get("domain") or item.get("consumer") or "").strip(),
            )
            for item in unreachable
            if isinstance(item, dict)
            and str(item.get("domain") or item.get("consumer") or "").strip()
        )
        if entries:
            found[f"resource:{resource.key}"] = entries
            blocked.extend(
                (f"resource:{resource.key}", item)
                for item in unreachable
                if isinstance(item, dict) and str(item.get("endpoint", "")).strip()
            )

    # Why it could not be reached, where the tailnet policy is the answer.
    #
    # HQ can decide whether one machine may reach another on a port, so the
    # answer arrives with the failure instead of waiting to be looked up.
    #
    # Paid for only when something is actually unreachable, the way the zone
    # facts below refuse to buy a query to learn there are no domains.
    if blocked:
        found = _policy_verdicts(found, blocked)

    # Nothing further when the estate holds no zone, the way `_measure` pays
    # nothing when nothing is named like a host. This runs inside the shared
    # projection that the dashboard budget measures, so a deployment with no
    # domains must not buy a query to learn it has none.
    zones = tuple(
        resource for resource in resources if resource.kind == ZONE_KIND
    )
    if not zones:
        return found

    from .facts import Subject, inventory_about

    for resource in zones:
        name = normalized_hostname(resource.spec.get("zone"))
        registration: dict[str, Any] = {}
        for _snapshot, record in inventory_about(ZONE_KIND, Subject.of(hostnames=(name,))):
            registration = dict(record.get("registration") or {})
        if not registration or registration.get("unread"):
            continue
        # Added to, never over: the unreachable consumers above are kept.
        found[f"resource:{resource.key}"] = found.get(
            f"resource:{resource.key}", ()
        ) + (
            ("domain", name),
            ("expires_at", str(registration.get("expires_at", ""))),
            ("auto_renew", "yes" if registration.get("auto_renew") else "no"),
            ("registrar", str(registration.get("registrar", ""))),
        )
    return found


def add_connection_facts(nodes: dict[str, TopologyNode]) -> None:
    """Facts a connection node carries for the findings that read them.

    What each edge relies on to stay shut (joined on the connection's ref), a
    credential its provider refused or that lacks permissions or is expiring,
    with its fix, work the last pass could not finish, the tailnet's own
    readings on the connections of the tailnet's providers, and what each
    reading's ``facts`` say about the connection that took it.
    """

    from .connections import unfinished_work
    from .credential_findings import credential_facts
    from .facts import connection_facts
    from .credential_mint import credential_fixes
    from .estate import refused_connections
    from .tailnet import TAILNET_KIND, posture_facts

    perimeter = _perimeter_facts()
    unanswered = _unanswered()
    refused = refused_connections()
    fixes = credential_fixes()
    unfinished = unfinished_work()
    tailnet = _tailnet_facts() + posture_facts()
    tailnet_providers = PROVIDERS[TAILNET_KIND].connection_providers

    def facts_for(node: TopologyNode) -> tuple[tuple[str, str], ...]:
        found = tuple(perimeter.get(node.connection_ref, ()))
        found += unanswered.get((node.controller_id, node.connection_ref), ())
        if node.connection_ref in refused:
            found += (("credential-refused", refused[node.connection_ref]),)
        found += credential_facts(fixes.get(node.connection_ref))
        steps = unfinished.get((node.controller_id, node.connection_ref), ())
        found += tuple(("work-unfinished", step) for step in steps)
        if node.provider in tailnet_providers:
            found += tailnet
        found += connection_facts(node.connection_ref, node.provider)
        return found

    for node_id, node in list(nodes.items()):
        if node.kind != "connection":
            continue
        extra = facts_for(node)
        if extra:
            nodes[node_id] = replace(node, facts=node.facts + extra)


def _unanswered() -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    """Why each connection that did not answer failed, and where it points."""

    from .connections import connection_rows
    from .credential_findings import ENDPOINT, FAILURE

    return {
        (row.controller_id, row.connection_ref): (
            *(((FAILURE, row.failure),) if row.failure else ()),
            *(((ENDPOINT, row.endpoint),) if row.endpoint else ()),
        )
        for row in connection_rows()
        if not row.reachable
    }
