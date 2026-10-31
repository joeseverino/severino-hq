"""What the connections themselves report, attached to the nodes they read: the perimeter, the tailnet, and the policy each observation is judged against."""

import re
from dataclasses import replace
from typing import Any

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.observations.tailscale import EXIT_ROUTES
from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_KIND
from hq.domains.control_plane.providers import PROVIDERS
lazy from hq.domains.control_plane.observations.host import PERIMETER_KIND
lazy from hq.platform.core.network import parse_ip

from .topology_model import TopologyNode
lazy from . import tailnet
lazy from .conditions import held_since
lazy from .connections import connection_rows, unfinished_work
lazy from .credential_findings import ENDPOINT, FAILURE, UNREPORTED, credential_facts
lazy from .credential_mint import credential_fixes
lazy from .estate import refused_connections
lazy from .facts import Subject, connection_facts, inventory_about, snapshots_of
lazy from .history import near
lazy from .moments import ago
lazy from .tailnet import device_at, may_reach, observer, posture_facts
lazy from .zones import ZONE_KIND


def _inventory_of(kind: str) -> tuple[Any, ...]:
    """Snapshots of one kind, from the join engine's one read of the inventory."""

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
            entries.extend(("answers-publicly", str(port)) for port in record.get("answered_publicly") or ())
            if entries:
                found[connection_ref] = tuple(entries)
    return found


def _tailnet_facts() -> tuple[tuple[str, str], ...]:
    """What the tailnet uses, and each global resolver that is not part of it.

    Addresses are the IPv4 addresses of every device the device reading holds;
    routes are the subnet routes approved for them, exit routes excluded.
    Nothing is said without a device reading, since every claim here compares
    against it.
    """

    addresses, routes = _tailnet_reach()
    if not addresses:
        return ()
    return (
        *(("tailnet-dns-off-tailnet", address) for address in _resolvers_off(addresses)),
        *(("tailnet-address", address) for address in sorted(addresses) if parse_ip(address) is not None),
        *(("tailnet-route", route) for route in sorted(routes)),
    )


def _tailnet_reach() -> tuple[set[str], set[str]]:
    """Every device's addresses, and the subnet routes approved for them, exit routes excluded."""

    addresses: set[str] = set()
    routes: set[str] = set()
    for snapshot in _inventory_of(TAILNET_KIND):
        for record in snapshot.records:
            addresses.update(str(item) for item in record.get("addresses") or ())
            routes.update(str(route) for route in record.get("enabled_routes") or () if str(route) not in EXIT_ROUTES)
    return addresses, routes


def _resolvers_off(addresses: set[str]) -> list[str]:
    """The global resolvers the tailnet's DNS reading names that are no device's address."""

    return [
        str(address)
        for snapshot in _inventory_of("tailscale.dns")
        for record in snapshot.records
        for address in record.get("nameservers") or ()
        if parse_ip(str(address)) is not None and str(address) not in addresses
    ]


def _policy_verdicts(
    found: dict[str, tuple[tuple[str, str], ...]],
    blocked: list[tuple[str, dict[str, str]]],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Add, for each unreachable address, whether the tailnet is what refused.

    Only a known refusal is this fact. A path the policy admits says nothing
    (the consumer is down or not listening), and neither does a tailnet HQ has
    not swept, which would send an operator to change a policy that was never
    the problem.
    """

    known = tailnet.devices()
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
        found[node_id] = (
            *found.get(node_id, ()),
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

    found: dict[str, tuple[tuple[str, str], ...]] = {}

    # Already in hand, so this costs nothing: the sweep wrote it into the
    # status this function was handed.
    blocked: list[tuple[str, dict[str, str]]] = []
    for resource in resources:
        entries, endpoints = unreachable_consumers(resource)
        if entries:
            found[f"resource:{resource.key}"] = entries
            blocked.extend((f"resource:{resource.key}", item) for item in endpoints)

    # Why it could not be reached, where the tailnet policy is the answer.
    #
    # HQ can decide whether one machine may reach another on a port, so the
    # answer arrives with the failure instead of waiting to be looked up.
    #
    # Paid for only when something is actually unreachable, the way the zone
    # facts below refuse to buy a query to learn there are no domains.
    if blocked:
        found = _policy_verdicts(found, blocked)

    # When a drifted declaration was first seen drifting, and what happened
    # near then: the deploy, recreate or edit that likely did it. Paid for only
    # when something has drifted.
    for resource in resources:
        _add_to(found, resource, _drift_facts(resource))

    # Nothing further when the estate holds no zone, the way `_measure` pays
    # nothing when nothing is named like a host. This runs inside the shared
    # projection that the dashboard budget measures, so a deployment with no
    # domains must not buy a query to learn it has none.
    zones = tuple(resource for resource in resources if resource.kind == ZONE_KIND)
    if not zones:
        return found

    for resource in zones:
        _add_to(found, resource, _registration_facts(resource))
    return found


def unreachable_consumers(resource) -> tuple[tuple[tuple[str, str], ...], tuple[dict[str, str], ...]]:
    """The consumers a sweep could not reach, as ``unreachable`` facts, and
    those of them that name the endpoint it tried."""

    unreachable = (resource.status or {}).get("unreachable_consumers") or []
    if not isinstance(unreachable, list):
        return (), ()
    consumers = [item for item in unreachable if isinstance(item, dict)]
    entries = tuple(
        ("unreachable", name)
        for name in (str(item.get("domain") or item.get("consumer") or "").strip() for item in consumers)
        if name
    )
    return entries, tuple(item for item in consumers if str(item.get("endpoint", "")).strip())


def _registration_facts(zone) -> tuple[tuple[str, str], ...]:
    """A zone's registration as the zone sweep last read it, or nothing when it was not read."""

    name = normalized_hostname(zone.spec.get("zone"))
    registration: dict[str, Any] = {}
    for _snapshot, record in inventory_about(ZONE_KIND, Subject.of(hostnames=(name,))):
        registration = dict(record.get("registration") or {})
    if not registration or registration.get("unread"):
        return ()
    return (
        ("domain", name),
        ("expires_at", str(registration.get("expires_at", ""))),
        ("auto_renew", "yes" if registration.get("auto_renew") else "no"),
        ("registrar", str(registration.get("registrar", ""))),
    )


def _add_to(found: dict[str, tuple[tuple[str, str], ...]], resource, entries: tuple[tuple[str, str], ...]) -> None:
    """Add ``entries`` to a resource's facts, never over what it already holds."""

    if entries:
        found[f"resource:{resource.key}"] = (*found.get(f"resource:{resource.key}", ()), *entries)


def _drift_facts(resource) -> tuple[tuple[str, str], ...]:
    since = held_since(resource.conditions, "Drifted")
    if since is None:
        return ()
    # What a person or another system did. HQ's own record of reading and
    # applying this record is the check that found the change, never its cause.
    others = [item for item in near(since, limit=20) if not _own_bookkeeping(item, resource.key)]
    return (
        ("drift-since", since.isoformat()),
        *(("drift-near", f"{item.title} ({ago(item.at)})") for item in others[:5]),
    )


_OPERATION_STATE = re.compile(r"\((queued|claimed|succeeded|failed)\)$")


def _own_bookkeeping(item, key: str) -> bool:
    return item.source == "HQ" and (item.title.endswith(f" {key}") or _OPERATION_STATE.search(item.title) is not None)


def add_connection_facts(nodes: dict[str, TopologyNode]) -> None:
    """Facts a connection node carries for the findings that read them.

    What each edge relies on to stay shut (joined on the connection's ref), a
    credential its provider refused or that lacks permissions or is expiring,
    with its fix, work the last pass could not finish, the tailnet's own
    readings on the connections of the tailnet's providers, and what each
    reading's ``facts`` say about the connection that took it.
    """

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
    """Why each connection that did not answer failed, and where it points.

    One its controller stopped reporting says when instead of why.
    """

    return {
        (row.controller_id, row.connection_ref): (
            *(((UNREPORTED, row.unreported_at.isoformat()),) if row.unreported_at else ()),
            *(((FAILURE, row.failure),) if row.failure else ()),
            *(((ENDPOINT, row.endpoint),) if row.endpoint else ()),
        )
        for row in connection_rows()
        if not row.reachable
    }
