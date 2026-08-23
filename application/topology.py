"""A live infrastructure topology derived from HQ's canonical contracts.

Nothing in this module is persisted. Connections are observations, resources
are declarations, and abilities come from the provider registry; this merely
joins them into nodes and edges for every delivery adapter. Manipulation is a
link to an existing application capability or web use case, never a graph-only
mutation that could drift from the thing it claims to represent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

from django.urls import reverse

from control_plane.models import ManagedResource, OperationRequest
from control_plane.providers import CERTIFICATE_KIND, PROVIDERS, controller_action_policy

from .connections import ConnectionGroup, ConnectionLink, connection_catalog
from .infrastructure import certificate_renewal_allowed, resource_health
from .security import AuthorizationError, Capability, Principal


@dataclass(frozen=True)
class TopologyAction:
    """One safe way to inspect or change the canonical object behind a node."""

    name: str
    label: str
    effect: str
    url: str
    method: str = "GET"
    capability: str = ""
    target: str = ""


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


@dataclass(frozen=True)
class Topology:
    """The complete permitted projection consumed by web, API, and MCP."""

    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]


_KIND_ORDER = {
    "controller": 0,
    "connection": 1,
    "ability": 2,
    "resource": 3,
    "target": 4,
    "dependency": 5,
}


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
    return f"{reverse('control_plane:topology')}?{urlencode({'focus': node_id})}#map"


def _edge(source: str, target: str, kind: str, label: str, status="neutral"):
    return TopologyEdge(
        id=_derived_id("edge", source, target, kind),
        source=source,
        target=target,
        kind=kind,
        label=label,
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
    resource: ManagedResource, principal: Principal
) -> tuple[TopologyAction, ...]:
    key = resource.key
    actions = [
        TopologyAction(
            "open",
            "Open",
            "read",
            reverse("control_plane:detail", kwargs={"key": key}),
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
    if resource.enabled:
        reconcile, _ = controller_action_policy(
            resource.kind, OperationRequest.Action.RECONCILE
        )
        if reconcile:
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
        if (
            resource.kind == CERTIFICATE_KIND
            and _permitted(principal, Capability.REQUEST_CERTIFICATE_RENEWAL)
            and certificate_renewal_allowed(resource)[0]
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
    actions.append(
        TopologyAction(
            "remove",
            "Review removal",
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


def _connection_nodes(
    groups: tuple[ConnectionGroup, ...],
    nodes: dict[str, TopologyNode],
    edges: dict[str, TopologyEdge],
) -> None:
    for group in groups:
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
                    actions=(
                        TopologyAction(
                            "focus",
                            "Show relationships",
                            "read",
                            _focus_url(ability_id),
                        ),
                    ),
                ),
            )
        for connection in group.connections:
            instance = connection.instance
            connection_id = f"connection:{group.spec.name}:{instance.id}"
            connection_url = reverse("control_plane:connections")
            nodes[connection_id] = TopologyNode(
                id=connection_id,
                kind="connection",
                label=instance.label,
                subtitle=instance.kind,
                status=instance.status,
                status_label=instance.status_label,
                detail=instance.detail,
                url=connection_url,
                actions=(
                    TopologyAction("open", "Open connections", "read", connection_url),
                ),
            )
            if instance.controller_id:
                controller_id = _derived_id("controller", instance.controller_id)
                nodes.setdefault(
                    controller_id,
                    TopologyNode(
                        id=controller_id,
                        kind="controller",
                        label=instance.controller_id,
                        subtitle="Controller",
                        url=connection_url,
                        actions=(
                            TopologyAction(
                                "open", "Open connections", "read", connection_url
                            ),
                        ),
                    ),
                )
                relation = _edge(
                    controller_id, connection_id, "carries", "Carries"
                )
                edges[relation.id] = relation
            for target in instance.targets:
                node = _link_node(target, kind="target")
                nodes.setdefault(node.id, node)
                relation = _edge(
                    connection_id, node.id, "reaches", "Reaches", instance.status
                )
                edges[relation.id] = relation
            for dependency in instance.dependencies:
                resource_id = f"resource:{dependency.resource_key}"
                if dependency.resource_key and resource_id in nodes:
                    target_id = resource_id
                else:
                    node = _link_node(dependency, kind="dependency")
                    nodes.setdefault(node.id, node)
                    target_id = node.id
                relation = _edge(
                    connection_id, target_id, "used_by", "Used by", instance.status
                )
                edges[relation.id] = relation
            for state in connection.abilities:
                ability = state.ability
                ability_id = f"ability:{group.spec.name}:{ability.name}"
                available = (
                    "good"
                    if state.available is True
                    else "serious" if state.available is False else "neutral"
                )
                relation = _edge(
                    connection_id, ability_id, "enables", "Enables", available
                )
                edges[relation.id] = relation


def derive_topology(*, principal: Principal) -> Topology:
    """Derive the complete topology visible to ``principal`` from live state."""

    principal.require(Capability.READ)
    nodes: dict[str, TopologyNode] = {}
    edges: dict[str, TopologyEdge] = {}
    resources = tuple(ManagedResource.objects.all())
    for resource in resources:
        provider = PROVIDERS.get(resource.kind)
        status, status_label, detail = _resource_status(resource)
        resource_id = f"resource:{resource.key}"
        nodes[resource_id] = TopologyNode(
            id=resource_id,
            kind="resource",
            label=resource.key,
            subtitle=(provider.label if provider and provider.label else resource.kind),
            status=status,
            status_label=status_label,
            detail=detail,
            url=reverse("control_plane:detail", kwargs={"key": resource.key}),
            actions=_resource_actions(resource, principal),
        )

    groups = connection_catalog(principal=principal)
    _connection_nodes(groups, nodes, edges)

    resources_by_kind: dict[str, list[str]] = {}
    for resource in resources:
        resources_by_kind.setdefault(resource.kind, []).append(
            f"resource:{resource.key}"
        )
    for group in groups:
        for ability in group.spec.abilities:
            ability_id = f"ability:{group.spec.name}:{ability.name}"
            for kind in ability.governs_kinds:
                for resource_id in resources_by_kind.get(kind, ()):
                    relation = _edge(
                        ability_id, resource_id, "governs", "Governs"
                    )
                    edges[relation.id] = relation

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


def serialize_topology(topology: Topology) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for node in topology.nodes:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    return {
        "ok": True,
        "schema_version": 1,
        "summary": {
            "nodes": len(topology.nodes),
            "edges": len(topology.edges),
            "kinds": dict(sorted(counts.items())),
        },
        "nodes": [asdict(node) for node in topology.nodes],
        "edges": [asdict(edge) for edge in topology.edges],
    }


def topology(*, principal: Principal) -> dict[str, Any]:
    """Return the shared serialized projection for machine delivery adapters."""

    return serialize_topology(derive_topology(principal=principal))
