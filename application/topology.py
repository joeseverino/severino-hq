"""A live infrastructure topology derived from HQ's canonical contracts.

Nothing in this module is persisted. Connections are observations, resources
are declarations, and abilities come from the provider registry; this merely
joins them into nodes and edges for every delivery adapter. Manipulation is a
link to an existing application capability or web use case, never a graph-only
mutation that could drift from the thing it claims to represent.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Callable
from django.urls import reverse

from control_plane.names import normalized_hostname
from control_plane.models import ManagedResource
from control_plane.providers import CONNECTION_LABELS, PROVIDERS

from .analytics import HOST_TRAFFIC_DAYS, traffic_for_hosts
from .connections import (
    ConnectionGroup,
    ConnectionLink,
    ConnectionSpec,
    connection_catalog,
)
from .action_links import ActionLink as TopologyAction
from .action_links import capability_action_link, connection_action_links, topology_url
from .entity_links import EntityLink, entity_link, kind_label
from .infrastructure import resource_health
from .resource_capabilities import removals_pending, resource_capabilities
from .security import AuthorizationError, Capability, Principal


@dataclass(frozen=True)
class TopologyNode:
    """One addressable thing in the derived topology."""

    id: str
    kind: str
    label: str
    subtitle: str
    status: str = "neutral"
    status_label: str = ""
    detail: str = ""
    url: str = ""
    # What this node is an instance of: a provider kind, or the connection
    # family that emitted it. The subtitle already reads as this, but a subtitle
    # is a rendered label and must never become a join key; grouping siblings
    # needs identity.
    kind_key: str = ""
    # A connection node's provider (``tailscale``, ``cloudflare_api``): the join
    # key its readings are matched by. The subtitle is that provider's label.
    provider: str = ""
    # A connection node's ref and the controller that reported it: the join keys
    # for facts about the connection. A ref is unique per controller only.
    connection_ref: str = ""
    controller_id: str = ""
    # When this was last observed, ISO 8601, or "" when nothing observes it.
    # Health describes the content of the last observation and says nothing
    # about its age, so a thing observed once and never again reads healthy
    # forever. This is the fact that distinguishes the two.
    observed_at: str = ""
    # What was asked for, and what was last confirmed back. Between them the
    # whole of triage: equal means a reconcile already ran against this exact
    # declaration, so a difference the world still shows is the declaration
    # being wrong rather than the convergence being late.
    declared_revision: int = 0
    observed_revision: int = 0
    # The reason on the active condition, verbatim. "Observed" was written by a
    # sweep that saw this; "Reconciled" was written by a reconcile and says
    # nothing about whether a sweep has confirmed it since.
    reason: str = ""
    # Whether HQ is converging this at all. A disabled declaration is not a
    # finding: nobody asked for it to be true.
    managed: bool = True
    # Declared as usually absent, so a sweep not finding it is expected.
    on_demand: bool = False
    # Fields this declaration asserts that the last observation did not echo
    # back, excluding the ones the provider declared it cannot report. Drift is
    # compared only across fields present in both, so a field the reading omits
    # is unverified rather than agreed: the difference between "we set this"
    # and "we checked this".
    unconfirmed_fields: tuple[str, ...] = ()
    # Facts a sweep reports that no declaration carries, as flat strings. A
    # findings rule derives from this topology and is not allowed a query of its
    # own (the suite measures that) so an observation a rule has to reason
    # about has to arrive here or not at all.
    #
    # Flat and small on purpose. This is not a second copy of the inventory; it
    # is the handful of observed facts that decide something.
    facts: tuple[tuple[str, str], ...] = ()
    # What this name actually served, where anything measures it. ``None`` is
    # not zero: nobody visited and nobody looked are opposite findings, and the
    # second is the one worth acting on: a target HQ reaches, and nothing
    # measures, is a site running unobserved.
    pageviews: int | None = None
    visits: int | None = None
    actions: tuple[TopologyAction, ...] = ()


@dataclass(frozen=True)
class TopologyEdge:
    """A relationship derived from a declaration or observation."""

    id: str
    source: str
    target: str
    kind: str
    label: str
    status: str = "neutral"
    # For an edge a reading supports: the reading kind, what it names, and
    # when it was read. An edge exists only while its reading does.
    source_kind: str = ""
    detail: str = ""
    observed_at: str = ""
    # For a reading edge: each record it stands for, through the link builder,
    # and the facet the reading supplies.
    entities: tuple[EntityLink, ...] = ()
    facet: str = ""


@dataclass(frozen=True)
class RelationKind:
    """What an edge kind says from each end, and where it ranks on a page.

    ``phrase`` reads from the source, ``inverse`` from the target. A lower
    ``rank`` is shown first.
    """

    phrase: str
    inverse: str
    rank: int


# Every structural edge kind, stated once. Reading edges take their phrase from
# the reading's ``relation`` and their rank from its facet.
RELATIONS: dict[str, RelationKind] = {
    "runs_on": RelationKind("Runs on", "Serves", 10),
    "runs": RelationKind("Runs", "Runs on", 15),
    "contains": RelationKind("Contains", "In domain", 30),
    "reaches": RelationKind("Reaches", "Reached through", 70),
    "on_tailnet": RelationKind("On the tailnet as", "Tailnet device of", 75),
    "declared_by": RelationKind("Declared by", "Declares", 80),
    "carries": RelationKind("Carries", "Carried by", 85),
    "used_by": RelationKind("Used by", "Uses", 85),
    "enables": RelationKind("Enables", "Enabled by", 85),
    "governs": RelationKind("Governs", "Governed by", 85),
    "reading": RelationKind("", "Reads", 88),
}

# Where a reading's relation ranks, by the facet it supplies. What serves a
# name comes first; a protective overlay with no facet (Access) comes last.
READING_RANKS: dict[str, int] = {
    "runtime": 20,
    "network": 20,
    "dns": 35,
    "proxy": 40,
    "certificate": 50,
    "registration": 60,
    "": 90,
}


def relation_rank(edge: "TopologyEdge") -> int:
    """Where an edge's relation is shown among a node's relationships."""

    if edge.kind == "reading":
        return READING_RANKS.get(edge.facet, READING_RANKS[""])
    relation = RELATIONS.get(edge.kind)
    return relation.rank if relation else 99


@dataclass(frozen=True)
class Topology:
    """The complete permitted projection consumed by web, API, and MCP."""

    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]


@dataclass(frozen=True)
class TopologyTrace:
    """A bounded traversal applied to an already-authorized topology."""

    focus: str
    direction: str
    depth: int
    hops: tuple[tuple[str, int], ...]


_KIND_ORDER = {
    "controller": 0,
    "connection": 1,
    "machine": 2,
    "service": 3,
    "zone": 4,
    "ability": 5,
    "resource": 6,
    "registry": 7,
    "target": 8,
    "dependency": 9,
}

# Node kinds whose ``observed_at`` is the newest of the readings joined to
# them rather than one sweep's stamp, so siblings are not compared by it.
JOINED_KINDS = frozenset({"machine", "service", "zone", "registry", "controller"})

# Node kinds a sweep or a reading can observe. A declaration is observable
# unless its provider says no sweep reads it.
_OBSERVABLE_KINDS = frozenset({"connection", *JOINED_KINDS})


def observable(node: TopologyNode) -> bool:
    """Whether anything can observe this node, so "never observed" is a gap."""

    if node.kind == "resource":
        provider = PROVIDERS.get(node.kind_key)
        return not (provider and provider.unobserved_reason)
    return node.kind in _OBSERVABLE_KINDS


def newest_stamp(*stamps: str) -> str:
    """The latest of several ISO 8601 instants, or ""."""

    moments = []
    for stamp in stamps:
        try:
            moment = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            continue
        if moment is not None:
            moments.append(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    return max(moments).isoformat() if moments else ""


TRACE_DIRECTIONS = ("inbound", "outbound", "both")
MAX_TRACE_DEPTH = 5


def _permitted(principal: Principal, capability: Capability | str) -> bool:
    try:
        principal.require(capability)
    except AuthorizationError:
        return False
    return True


def _derived_id(kind: str, *parts: str) -> str:
    """Keep composite observation ids stable and compact, not confidential."""

    digest = sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


def _focus_url(node_id: str) -> str:
    return topology_url(node_id)


def _edge(source: str, target: str, kind: str, label: str = "", status="neutral"):
    return TopologyEdge(
        id=_derived_id("edge", source, target, kind),
        source=source,
        target=target,
        kind=kind,
        label=label or RELATIONS[kind].phrase,
        status=status,
    )


def _resource_status(resource: ManagedResource) -> tuple[str, str, str]:
    if not resource.enabled:
        return "neutral", "Disabled", "This declaration is not reconciled."
    health = resource_health(resource)
    state = {
        "healthy": "good",
        "declared": "good",
        "pending": "attention",
        "drifted": "serious",
        "degraded": "serious",
    }.get(health["state"], "neutral")
    return state, health["label"], health["message"]


def _resource_actions(
    resource: ManagedResource, principal: Principal, removal_pending: bool, manages
) -> tuple[TopologyAction, ...]:
    key = resource.key
    actions = [
        TopologyAction(
            "open",
            "Open",
            "read",
            resource.get_absolute_url(),
        )
    ]
    if not _permitted(principal, Capability.MANAGE_INFRASTRUCTURE):
        return tuple(actions)
    actions.append(
        TopologyAction(
            "edit",
            "Edit declaration",
            "remote_write",
            reverse("control_plane:edit", kwargs={"key": key}),
            capability="infrastructure.resource.update",
            target=key,
        )
    )
    capabilities = resource_capabilities(
        resource, running=(), removal_pending=removal_pending, manages=manages
    )
    reconcile = capabilities.actions.get("reconcile")
    if reconcile and reconcile.enabled:
        actions.append(
            TopologyAction(
                "reconcile",
                "Reconcile",
                "infrastructure_change",
                reverse("control_plane:reconcile", kwargs={"key": key}),
                method="POST",
                capability="infrastructure.reconcile",
                target=key,
            )
        )
    renew = capabilities.actions.get("renew")
    if (
        renew
        and renew.enabled
        and _permitted(principal, Capability.REQUEST_CERTIFICATE_RENEWAL)
    ):
        actions.append(
            TopologyAction(
                "renew",
                "Renew certificate",
                "infrastructure_change",
                reverse("control_plane:renew", kwargs={"key": key}),
                method="POST",
                capability="certificate.renew",
                target=key,
            )
        )
    if capabilities.removal != "unavailable":
        actions.append(
            TopologyAction(
                "remove",
                "Stop managing" if capabilities.removal == "forget" else "Review removal",
                "destructive",
                reverse("control_plane:remove", kwargs={"key": key}),
                capability="infrastructure.resource.remove",
                target=key,
            )
        )
    return tuple(actions)


def _link_node(link: ConnectionLink, *, kind: str) -> TopologyNode:
    node_id = _derived_id(kind, link.url, link.label)
    return TopologyNode(
        id=node_id,
        kind=kind,
        label=link.label,
        subtitle="Observed target" if kind == "target" else "Declared dependency",
        url=link.url,
        actions=(
            (TopologyAction("open", "Open", "read", link.url),) if link.url else ()
        ),
    )


def _connection_actions(spec: ConnectionSpec) -> tuple[TopologyAction, ...]:
    """Derive a connection's actions from what its spec declared, and no more.

    Every one is a link to a page that enforces its own authorization. The
    effect stays ``read`` because that is all this projection can honestly
    claim: the spec names a destination, not what an operator will do there.
    """

    return connection_action_links(spec)


def _ability_actions(
    ability, ability_id: str, principal: Principal
) -> tuple[TopologyAction, ...]:
    """Relate an ability to the graph, and to the capability it names.

    An ability that declares a capability is describing an executable contract
    HQ already owns. Naming it makes the relationship machine-readable without
    the graph acquiring a way to run it.
    """

    actions = [
        TopologyAction("focus", "Show relationships", "read", _focus_url(ability_id))
    ]
    command = capability_action_link(
        ability.capability,
        ability.effect,
        "Open command",
        principal=principal,
    )
    if command is not None:
        actions.append(command)
    return tuple(actions)


def _asserts_nothing(value: Any) -> bool:
    """Whether a declared value is the absence of a claim rather than a claim.

    A spec is a full model dump, so every optional field a provider defines is
    a key whether or not anyone set one. A TXT record carries a ``priority``
    key because MX records exist; ``None`` there is the schema showing through.

    ``False`` and ``0`` are claims, not absence: a device declaring key expiry
    *on*, or an MX at priority zero, has said something checkable.
    """

    return value is None or value == "" or value == [] or value == {}


def _unconfirmed(resource: ManagedResource, provider) -> tuple[str, ...]:
    """What this declaration asserts that the last reading did not echo back.

    Drift is compared only across fields present in *both*, so a field the
    reading omits is never judged: it is unverified rather than agreed.
    Fields the provider declared it cannot report are excluded: those are a
    known gap rather than a silent one. And a field carrying no value is
    excluded because there is nothing there to confirm.

    Without that last clause every DNS record that is not an MX asserted an
    unconfirmed ``priority``: twenty-eight of them, none clearable, since the
    provider correctly declines to read a priority back for a type that has
    none. They buried the findings that were real.
    """

    if not resource.last_observed_at or not isinstance(resource.status, dict):
        return ()
    # A provider with no ``from_record`` cannot turn a reading into a spec, so
    # no field of that spec is ever echoed back. A certificate declares what was
    # asked for (which name, which domains, where to install) and its
    # reading reports what exists: issuer, expiry, the PEM. Two vocabularies
    # that were never meant to overlap, and a per-field exemption list for them
    # is a list that goes stale.
    if provider is not None and not provider.from_record:
        return ()
    unobservable = set(getattr(provider, "unobservable_fields", ()) or ())
    return tuple(
        sorted(
            field
            for field, value in (resource.spec or {}).items()
            if field not in resource.status
            and field not in unobservable
            and not _asserts_nothing(value)
        )
    )


def _merge_controller_node(
    nodes: dict[str, TopologyNode],
    *,
    node_id: str,
    label: str,
    group_label: str,
    url: str,
    principal: Principal,
    observed_at: str = "",
) -> None:
    """Join every distinct emitted connection workflow onto one controller.

    Its observed time is the newest of the connections it reports.
    """

    action = TopologyAction("open", f"Open {group_label}", "read", url) if url else None
    refresh = capability_action_link(
        "infrastructure.controller.refresh",
        "infrastructure_change",
        "Request fresh sweep",
        principal=principal,
    )
    emitted = tuple(item for item in (action, refresh) if item is not None)
    current = nodes.get(node_id)
    if current is None:
        nodes[node_id] = TopologyNode(
            id=node_id,
            kind="controller",
            label=label,
            subtitle="Controller",
            url=url,
            observed_at=observed_at,
            actions=emitted,
        )
    else:
        additions = tuple(
            item
            for item in emitted
            if all(existing.url != item.url for existing in current.actions)
        )
        nodes[node_id] = replace(
            current,
            observed_at=newest_stamp(current.observed_at, observed_at),
            actions=current.actions + additions,
        )


def _connection_nodes(
    groups: tuple[ConnectionGroup, ...],
    nodes: dict[str, TopologyNode],
    edges: dict[str, TopologyEdge],
    principal: Principal,
) -> None:
    for group in groups:
        spec_actions = _connection_actions(group.spec)
        connection_url = spec_actions[0].url if spec_actions else ""
        _declared_ability_nodes(group, nodes, principal)
        for connection in group.connections:
            instance = connection.instance
            node = _connection_node(group, instance, spec_actions, connection_url)
            nodes[node.id] = node
            if instance.controller_id:
                _controller_edge(group, instance, node, connection_url, nodes, edges, principal)
            _target_edges(instance, node.id, nodes, edges)
            _dependency_edges(instance, node.id, nodes, edges)
            _ability_edges(group, connection, node.id, edges)


def _declared_ability_nodes(group, nodes, principal) -> None:
    # A declared ability exists even when no controller currently reports a
    # matching connection. Keeping it in the graph makes the difference
    # between unsupported and temporarily unobserved explicit, and keeps
    # resources of that kind discoverable instead of orphaning them.
    for ability in group.spec.abilities:
        ability_id = f"ability:{group.spec.name}:{ability.name}"
        nodes.setdefault(
            ability_id,
            TopologyNode(
                id=ability_id,
                kind="ability",
                label=ability.label,
                subtitle=ability.name,
                detail=ability.summary,
                url=_focus_url(ability_id),
                actions=_ability_actions(ability, ability_id, principal),
            ),
        )


def _connection_node(group, instance, spec_actions, connection_url) -> TopologyNode:
    return TopologyNode(
        id=f"connection:{group.spec.name}:{instance.id}",
        kind="connection",
        label=instance.label,
        subtitle=CONNECTION_LABELS.get(instance.kind, group.spec.label),
        provider=instance.kind,
        connection_ref=instance.connection_ref,
        controller_id=instance.controller_id,
        status=instance.status,
        status_label=instance.status_label,
        detail=instance.detail,
        # The connection's row on the connections page, unless its
        # spec routes it to a page of its own.
        url=(
            entity_link("connection", instance.label).url
            if connection_url == reverse("control_plane:connections")
            else connection_url
        ),
        kind_key=group.spec.name,
        observed_at=(instance.observed_at.isoformat() if instance.observed_at else ""),
        actions=spec_actions,
    )


def _controller_edge(group, instance, node, connection_url, nodes, edges, principal) -> None:
    controller_id = _derived_id("controller", instance.controller_id)
    _merge_controller_node(
        nodes,
        node_id=controller_id,
        label=instance.controller_id,
        group_label=group.spec.label,
        url=connection_url,
        principal=principal,
        observed_at=node.observed_at,
    )
    relation = _edge(controller_id, node.id, "carries", "Carries")
    edges[relation.id] = relation


def _target_edges(instance, connection_id, nodes, edges) -> None:
    for target in instance.targets:
        node = _link_node(target, kind="target")
        nodes.setdefault(node.id, node)
        relation = _edge(connection_id, node.id, "reaches", "Reaches", instance.status)
        edges[relation.id] = relation
        # A target that is also a declaration using this connection.
        resource_id = f"resource:{target.resource_key}"
        if target.resource_key and resource_id in nodes:
            relation = _edge(
                connection_id, resource_id, "used_by", "Used by", instance.status
            )
            edges[relation.id] = relation


def _dependency_edges(instance, connection_id, nodes, edges) -> None:
    for dependency in instance.dependencies:
        resource_id = f"resource:{dependency.resource_key}"
        if dependency.resource_key and resource_id in nodes:
            target_id = resource_id
        else:
            node = _link_node(dependency, kind="dependency")
            nodes.setdefault(node.id, node)
            target_id = node.id
        relation = _edge(connection_id, target_id, "used_by", "Used by", instance.status)
        edges[relation.id] = relation


def _ability_edges(group, connection, connection_id, edges) -> None:
    for state in connection.abilities:
        ability_id = f"ability:{group.spec.name}:{state.ability.name}"
        available = (
            "good"
            if state.available is True
            else "serious" if state.available is False else "neutral"
        )
        relation = _edge(connection_id, ability_id, "enables", "Enables", available)
        edges[relation.id] = relation


# A label is a candidate hostname when it looks like one. Deliberately a shape
# test rather than a list of kinds: a target is a hostname, a resource key
# sometimes is, and an extension may emit a node kind this module has never
# heard of. Asking what the label *is* keeps that open.
_HOSTNAME_SHAPE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+\.?$")


def _measure(nodes: dict[str, TopologyNode]) -> None:
    """Give every node named like a host what that host actually served.

    The join is the name, the same one the service page uses: analytics stores
    a reading against a hostname and these nodes *are* hostnames, so no key
    ties them and none is stored twice.

    One query for the whole graph, and none at all when nothing in it is named
    like a host: a deployment measuring nothing pays nothing, which is what
    lets this sit in the shared projection rather than in one adapter.
    """

    candidates = {
        node.id: normalized_hostname(node.label)
        for node in nodes.values()
        if _HOSTNAME_SHAPE.match(node.label.strip().lower())
    }
    if not candidates:
        return
    measured = traffic_for_hosts(set(candidates.values()), days=HOST_TRAFFIC_DAYS)
    if not measured:
        return
    for node_id, host in candidates.items():
        reading = measured.get(host)
        if reading:
            nodes[node_id] = replace(
                nodes[node_id],
                pageviews=reading["pageviews"],
                visits=reading["visits"],
            )


def _inventory_of(kind: str) -> tuple[Any, ...]:
    """Snapshots of one kind, from the join engine's one read of the inventory."""

    from .facts import snapshots_of

    return snapshots_of(kind)


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
    for snapshot in _inventory_of("tailscale.device"):
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
        if getattr(parse_ip(address), "version", 0) == 4
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


def _observed_facts(
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


def derive_topology(*, principal: Principal, request: Any = None) -> Topology:
    """Derive the complete topology visible to ``principal`` from live state.

    ``request``, where there is one, says which address reached HQ.
    """

    from .hq_self import serving
    from .projection import projection_scope

    principal.require(Capability.READ)
    with projection_scope(seed=serving(request) if request is not None else None):
        return _derive(principal)


def _resource_node(resource: ManagedResource) -> TopologyNode:
    """A declaration as a node: its key, its kind's label, its page."""

    return TopologyNode(
        id=f"resource:{resource.key}",
        kind="resource",
        label=resource.key,
        subtitle=kind_label(resource.kind),
        url=resource.get_absolute_url(),
        kind_key=resource.kind,
    )


@dataclass(frozen=True)
class RelationGraph:
    """The estate's nodes and edges, and the join key of each estate node."""

    topology: Topology
    subjects: dict[str, Any]


def relation_graph(*, principal: Principal) -> RelationGraph:
    """Machines, services, domains, declarations and connections, and their edges.

    The topology's own node and edge code, without what only the topology page
    needs: health, actions, abilities' governance, traffic and perimeter facts.
    Read once per projection, so every page section shares one derivation.
    """

    from .projection import read_once
    from .topology_estate import add_estate

    principal.require(Capability.READ)

    def build() -> RelationGraph:
        nodes: dict[str, TopologyNode] = {}
        edges: dict[str, TopologyEdge] = {}
        resources = tuple(ManagedResource.objects.all())
        for resource in resources:
            node = _resource_node(resource)
            nodes[node.id] = node
        _connection_nodes(connection_catalog(principal=principal), nodes, edges, principal)
        subjects = add_estate(nodes, edges, resources)
        return RelationGraph(
            Topology(tuple(nodes.values()), tuple(edges.values())), subjects
        )

    capabilities = ",".join(sorted(str(item) for item in principal.capabilities))
    return read_once(
        f"topology.relations:{principal.actor}:{principal.interface}:{capabilities}", build
    )


def _derive(principal: Principal) -> Topology:
    from .topology_estate import add_estate

    edges: dict[str, TopologyEdge] = {}
    resources = tuple(ManagedResource.objects.all())
    nodes = _resource_nodes(resources, principal)
    # Observed facts that decide a finding, attached to the resource they are
    # about. Read once here rather than by a rule, which must cost no queries.
    for resource_id, extra in _observed_facts(resources).items():
        if resource_id in nodes:
            nodes[resource_id] = replace(nodes[resource_id], facts=extra)

    groups = connection_catalog(principal=principal)
    _connection_nodes(groups, nodes, edges, principal)
    add_estate(nodes, edges, resources)
    _add_connection_facts(nodes)
    _governs_edges(groups, resources, edges)
    _measure(nodes)

    ordered_nodes = tuple(
        sorted(
            nodes.values(),
            key=lambda node: (_KIND_ORDER.get(node.kind, 99), node.label.casefold(), node.id),
        )
    )
    ordered_edges = tuple(
        sorted(edges.values(), key=lambda edge: (edge.kind, edge.source, edge.target))
    )
    return Topology(ordered_nodes, ordered_edges)


def _resource_nodes(resources, principal: Principal) -> dict[str, TopologyNode]:
    """One node per declaration, with its state and what may be done to it."""

    from .adoption import manages_through

    # Only an operator is offered actions, so only an operator's view reads this.
    pending_removal = (
        removals_pending()
        if resources and _permitted(principal, Capability.MANAGE_INFRASTRUCTURE)
        else frozenset()
    )
    # Read on first use, once for every resource.
    manages = manages_through()
    nodes: dict[str, TopologyNode] = {}
    for resource in resources:
        provider = PROVIDERS.get(resource.kind)
        status, status_label, detail = _resource_status(resource)
        nodes[f"resource:{resource.key}"] = replace(
            _resource_node(resource),
            status=status,
            status_label=status_label,
            detail=detail,
            observed_at=(
                resource.last_observed_at.isoformat() if resource.last_observed_at else ""
            ),
            declared_revision=resource.generation,
            observed_revision=resource.observed_generation,
            reason=str((resource.conditions or [{}])[0].get("reason", "")).strip(),
            managed=resource.enabled,
            on_demand=bool((resource.spec or {}).get("on_demand")),
            unconfirmed_fields=_unconfirmed(resource, provider),
            actions=_resource_actions(
                resource, principal, resource.key in pending_removal, manages
            ),
        )
    return nodes


def _add_connection_facts(nodes: dict[str, TopologyNode]) -> None:
    """Facts a connection node carries for the findings that read them.

    What each edge relies on to stay shut (joined on the connection's ref), a
    credential its provider refused, work the last pass could not finish, and
    the tailnet's own readings on the connections of the tailnet's providers.
    """

    from .connections import unfinished_work
    from .estate import refused_connections
    from .tailnet import TAILNET_KIND, posture_facts

    perimeter = _perimeter_facts()
    refused = refused_connections()
    unfinished = unfinished_work()
    tailnet = _tailnet_facts() + posture_facts()
    tailnet_providers = PROVIDERS[TAILNET_KIND].connection_providers

    def facts_for(node: TopologyNode) -> tuple[tuple[str, str], ...]:
        found = tuple(perimeter.get(node.connection_ref, ()))
        if node.connection_ref in refused:
            found += (("credential-refused", refused[node.connection_ref]),)
        steps = unfinished.get((node.controller_id, node.connection_ref), ())
        found += tuple(("work-unfinished", step) for step in steps)
        if node.provider in tailnet_providers:
            found += tailnet
        return found

    for node_id, node in list(nodes.items()):
        if node.kind != "connection":
            continue
        extra = facts_for(node)
        if extra:
            nodes[node_id] = replace(node, facts=node.facts + extra)


def _governs_edges(groups, resources, edges: dict[str, TopologyEdge]) -> None:
    """An ability governs every declaration of the kinds it names."""

    resources_by_kind: dict[str, list[str]] = {}
    for resource in resources:
        resources_by_kind.setdefault(resource.kind, []).append(f"resource:{resource.key}")
    for group in groups:
        for ability in group.spec.abilities:
            ability_id = f"ability:{group.spec.name}:{ability.name}"
            for kind in ability.governs_kinds:
                for resource_id in resources_by_kind.get(kind, ()):
                    relation = _edge(ability_id, resource_id, "governs", "Governs")
                    edges[relation.id] = relation


@dataclass(frozen=True)
class TopologyLens:
    """A standing question about the graph, answered from the graph itself.

    A lens owns no inventory and runs no query. It selects ids out of a
    projection already derived and already authorized, so a lens can only ever
    narrow what a principal sees: never widen it.
    """

    name: str
    label: str
    summary: str
    select: Callable[[Topology], frozenset[str]]


_ATTENTION_STATES = frozenset({"attention", "serious"})

# How far behind its own kind's latest observation a node may fall before the
# gap means it was skipped rather than swept a moment later. Sweeps write one
# timestamp for everything they confirm, so siblings land together.
_STALE_AFTER = timedelta(hours=1)


def _incoming_kinds(topology: Topology) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = {}
    for edge in topology.edges:
        incoming.setdefault(edge.target, set()).add(edge.kind)
    return incoming


def _without_inbound(topology: Topology, kind: str, edge_kind: str) -> frozenset[str]:
    """Nodes of one kind that nothing currently relates to in one way.

    The absence is the finding: a resource nothing observes and a resource
    nothing governs are different gaps, and neither is visible from a node in
    isolation.
    """

    incoming = _incoming_kinds(topology)
    return frozenset(
        node.id
        for node in topology.nodes
        if node.kind == kind and edge_kind not in incoming.get(node.id, frozenset())
    )


def _needs_attention(topology: Topology) -> frozenset[str]:
    return frozenset(
        node.id for node in topology.nodes if node.status in _ATTENTION_STATES
    )


def _unobserved_resources(topology: Topology) -> frozenset[str]:
    return _without_inbound(topology, "resource", "used_by")


def _ungoverned_resources(topology: Topology) -> frozenset[str]:
    return _without_inbound(topology, "resource", "governs")


def _unobserved_abilities(topology: Topology) -> frozenset[str]:
    return _without_inbound(topology, "ability", "enables")


def _unresolved_dependencies(topology: Topology) -> frozenset[str]:
    return frozenset(node.id for node in topology.nodes if node.kind == "dependency")


def _stale_observations(topology: Topology) -> frozenset[str]:
    """Nodes a sweep passed over while it confirmed their siblings.

    Compared against the newest observation of the same ``kind_key`` rather
    than the clock. A kind on a slower cadence is not stale, it is slower, and
    an absolute threshold cannot tell those apart.
    """

    latest: dict[str, datetime] = {}
    seen: dict[str, datetime] = {}
    for node in topology.nodes:
        if not node.observed_at or not node.kind_key or node.kind in JOINED_KINDS:
            continue
        try:
            observed = datetime.fromisoformat(node.observed_at)
        except ValueError:
            continue
        seen[node.id] = observed
        newest = latest.get(node.kind_key)
        if newest is None or observed > newest:
            latest[node.kind_key] = observed
    return frozenset(
        node.id
        for node in topology.nodes
        if node.id in seen and latest[node.kind_key] - seen[node.id] > _STALE_AFTER
    )


def _isolated(topology: Topology) -> frozenset[str]:
    related: set[str] = set()
    for edge in topology.edges:
        related.add(edge.source)
        related.add(edge.target)
    return frozenset(node.id for node in topology.nodes if node.id not in related)


# Derived from node kinds and edge kinds alone, so an extension that emits a
# resource or an ability answers them without knowing they exist. Nothing here
# names a domain, a provider, or an installed package.
TOPOLOGY_LENSES: tuple[TopologyLens, ...] = (
    TopologyLens("attention", "Needs attention",
        "Everything currently reported as pending, drifted, degraded, or unreachable.",
        _needs_attention),
    TopologyLens("unobserved-resources", "Unreported resources",
        "Declared resources that no live connection currently names as a dependency.",
        _unobserved_resources),
    TopologyLens("ungoverned-resources", "Ungoverned resources",
        "Declared resources whose kind no connection ability claims to govern.",
        _ungoverned_resources),
    TopologyLens("unobserved-abilities", "Abilities with no live connection",
        "Abilities a provider declared that no current observation enables.",
        _unobserved_abilities),
    TopologyLens("unresolved-dependencies", "Unresolved dependencies",
        "Things a connection depends on that HQ holds no declaration for.",
        _unresolved_dependencies),
    TopologyLens("stale-observations", "Left behind by the last sweep",
        "Things observed materially longer ago than others of their own kind.",
        _stale_observations),
    TopologyLens("isolated", "Nodes with no relationships",
        "Anything nothing else currently reaches, governs, carries, or uses.",
        _isolated),
)

_LENS_BY_NAME = {lens.name: lens for lens in TOPOLOGY_LENSES}


def topology_lenses() -> tuple[TopologyLens, ...]:
    """Every standing question any adapter may ask of the topology."""

    return TOPOLOGY_LENSES


def lens_for(name: str) -> TopologyLens | None:
    """Resolve a requested lens, or ``None`` when no declaration claims it."""

    return _LENS_BY_NAME.get(name)


def apply_lens(topology: Topology, lens: TopologyLens) -> Topology:
    """Narrow a derived projection to one lens, keeping only surviving edges.

    An edge whose other end the lens excluded is dropped rather than left
    dangling: every adapter resolves an edge's endpoints against the node set.
    """

    selected = lens.select(topology)
    return Topology(
        tuple(node for node in topology.nodes if node.id in selected),
        tuple(
            edge
            for edge in topology.edges
            if edge.source in selected and edge.target in selected
        ),
    )


def apply_trace(
    topology: Topology,
    focus: str,
    *,
    direction: str = "both",
    depth: int | str = 2,
) -> tuple[Topology, TopologyTrace | None]:
    """Select a bounded dependency neighborhood without deriving new state.

    ``outbound`` follows the graph's declared source-to-target direction;
    ``inbound`` answers what points at the focus. Unknown inputs deliberately
    leave the projection unchanged and report no applied trace, matching the
    standing-lens contract used by every delivery adapter.
    """

    node_ids = {node.id for node in topology.nodes}
    if focus not in node_ids:
        return topology, None
    selected_direction = direction if direction in TRACE_DIRECTIONS else "both"
    try:
        selected_depth = int(depth)
    except (TypeError, ValueError):
        selected_depth = 2
    selected_depth = min(max(selected_depth, 1), MAX_TRACE_DEPTH)

    adjacent: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in topology.edges:
        if selected_direction in ("outbound", "both"):
            adjacent[edge.source].add(edge.target)
        if selected_direction in ("inbound", "both"):
            adjacent[edge.target].add(edge.source)

    hops = {focus: 0}
    frontier = {focus}
    for hop in range(1, selected_depth + 1):
        frontier = {
            neighbor
            for node_id in frontier
            for neighbor in adjacent[node_id]
            if neighbor not in hops
        }
        if not frontier:
            break
        hops.update({node_id: hop for node_id in frontier})

    narrowed = Topology(
        tuple(node for node in topology.nodes if node.id in hops),
        tuple(
            edge
            for edge in topology.edges
            if edge.source in hops and edge.target in hops
        ),
    )
    trace = TopologyTrace(
        focus=focus,
        direction=selected_direction,
        depth=selected_depth,
        hops=tuple(sorted(hops.items(), key=lambda item: (item[1], item[0]))),
    )
    return narrowed, trace


def serialize_topology(
    topology: Topology,
    *,
    lens: TopologyLens | None = None,
    trace: TopologyTrace | None = None,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for node in topology.nodes:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    return {
        "ok": True,
        "schema_version": 2,
        # Which lens produced this payload, and every lens that could have.
        "lens": lens.name if lens else None,
        "lenses": [
            {"name": item.name, "label": item.label, "summary": item.summary}
            for item in TOPOLOGY_LENSES
        ],
        "trace": (
            {
                "focus": trace.focus,
                "direction": trace.direction,
                "depth": trace.depth,
                "hops": [
                    {"node": node_id, "hop": hop}
                    for node_id, hop in trace.hops
                ],
            }
            if trace
            else None
        ),
        "summary": {
            "nodes": len(topology.nodes),
            "edges": len(topology.edges),
            "kinds": dict(sorted(counts.items())),
        },
        "nodes": [asdict(node) for node in topology.nodes],
        "edges": [asdict(edge) for edge in topology.edges],
    }


def topology(
    *,
    principal: Principal,
    lens: str = "",
    focus: str = "",
    direction: str = "both",
    depth: int | str = 2,
) -> dict[str, Any]:
    """Return the shared serialized projection for machine delivery adapters."""

    selected = lens_for(lens) if lens else None
    projection = derive_topology(principal=principal)
    if selected is not None:
        projection = apply_lens(projection, selected)
    projection, trace = apply_trace(
        projection, focus, direction=direction, depth=depth
    )
    return serialize_topology(projection, lens=selected, trace=trace)
