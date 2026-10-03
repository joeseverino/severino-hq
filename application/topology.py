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
from typing import Any
from application.routes import reverse

from control_plane.names import normalized_hostname
from control_plane.models import ManagedResource
from control_plane.providers import PROVIDERS
from control_plane.connection_kinds import CONNECTION_LABELS

from .analytics import HOST_TRAFFIC_DAYS, traffic_for_hosts
from .connections import ConnectionLink, ConnectionSpec
from .connection_catalog import ConnectionGroup, connection_catalog
from .action_links import (
    ActionLink as TopologyAction,
    capability_action_link,
    command_url,
    connection_action_links,
    topology_url,
)
from .entity_links import entity_link, kind_label
from .infrastructure import is_drifted, resource_health
from .resource_capabilities import removals_pending, resource_capabilities
from .security import Capability, Principal
from .topology_model import (
    Topology,
    TopologyEdge,
    TopologyNode,
    TopologyTrace,
    derived_id,
    edge_between,
    newest_stamp,
)
from .topology_lenses import TOPOLOGY_LENSES, TopologyLens, apply_lens, apply_trace, lens_for
from .contradiction_findings import add_contradiction_facts
from .topology_facts import add_connection_facts, add_observed_facts


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


def _focus_url(node_id: str) -> str:
    return topology_url(node_id)


def _resource_status(resource: ManagedResource) -> tuple[str, str, str]:
    if not resource.enabled:
        return "neutral", "Disabled", "This declaration is not reconciled."
    health = resource_health(resource)
    from .infrastructure import RESOURCE_TONES

    state = RESOURCE_TONES.get(health["state"], "neutral")
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
    if not principal.permits(Capability.MANAGE_INFRASTRUCTURE):
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
    drifted = is_drifted(resource)
    if drifted:
        # Something changed it outside HQ: keeping that is the choice that
        # loses nothing, so it is offered, and first.
        actions.append(
            TopologyAction(
                "keep_live",
                "Keep the live version",
                "remote_write",
                command_url("infrastructure.resource.accept_observed", key),
                capability="infrastructure.resource.accept_observed",
                target=key,
            )
        )
    reconcile = capabilities.actions.get("reconcile")
    if reconcile and reconcile.enabled:
        actions.append(
            TopologyAction(
                "reconcile",
                "Restore HQ's version" if drifted else "Reconcile",
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
        and principal.permits(Capability.REQUEST_CERTIFICATE_RENEWAL)
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
    node_id = derived_id(kind, link.url, link.label)
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

    Without that last clause every DNS record that is not an MX would assert
    an unconfirmed ``priority`` that nothing can clear, since the provider
    correctly declines to read a priority back for a type that has none.
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
    controller_id = derived_id("controller", instance.controller_id)
    _merge_controller_node(
        nodes,
        node_id=controller_id,
        label=instance.controller_id,
        group_label=group.spec.label,
        url=connection_url,
        principal=principal,
        observed_at=node.observed_at,
    )
    relation = edge_between(controller_id, node.id, "carries", "Carries")
    edges[relation.id] = relation


def _target_edges(instance, connection_id, nodes, edges) -> None:
    for target in instance.targets:
        node = _link_node(target, kind="target")
        nodes.setdefault(node.id, node)
        relation = edge_between(connection_id, node.id, "reaches", "Reaches", instance.status)
        edges[relation.id] = relation
        # A target that is also a declaration using this connection.
        resource_id = f"resource:{target.resource_key}"
        if target.resource_key and resource_id in nodes:
            relation = edge_between(
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
        relation = edge_between(connection_id, target_id, "used_by", "Used by", instance.status)
        edges[relation.id] = relation


def _ability_edges(group, connection, connection_id, edges) -> None:
    for state in connection.abilities:
        ability_id = f"ability:{group.spec.name}:{state.ability.name}"
        available = (
            "good"
            if state.available is True
            else "serious" if state.available is False else "neutral"
        )
        relation = edge_between(connection_id, ability_id, "enables", "Enables", available)
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
    """A declaration as a node: its name, its kind's label, its page.

    A container is named as itself, with its machine in the subtitle: its
    key joins the two, and printed as the name it would repeat the machine
    wherever the machine is already the context.
    """

    from control_plane.provider_adapters.portainer import CONTAINER_KIND

    name = str((resource.spec or {}).get("name", "") or "") if resource.kind == CONTAINER_KIND else ""
    host = str((resource.spec or {}).get("host", "") or "") if name else ""
    return TopologyNode(
        id=f"resource:{resource.key}",
        kind="resource",
        label=name or resource.key,
        subtitle=f"{kind_label(resource.kind)} on {host}" if host else kind_label(resource.kind),
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
    for resource_id, extra in add_observed_facts(resources).items():
        if resource_id in nodes:
            nodes[resource_id] = replace(nodes[resource_id], facts=extra)

    groups = connection_catalog(principal=principal)
    _connection_nodes(groups, nodes, edges, principal)
    add_estate(nodes, edges, resources)
    add_connection_facts(nodes)
    add_contradiction_facts(nodes)
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
        if resources and principal.permits(Capability.MANAGE_INFRASTRUCTURE)
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
                    relation = edge_between(ability_id, resource_id, "governs", "Governs")
                    edges[relation.id] = relation


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
