"""A live infrastructure topology derived from HQ's canonical contracts.

Nothing in this module is persisted. Connections are observations, resources
are declarations, and abilities come from the provider registry; this merely
joins them into nodes and edges for every delivery adapter. Manipulation is a
link to an existing application capability or web use case, never a graph-only
mutation that could drift from the thing it claims to represent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlencode

from django.urls import reverse

from control_plane.models import ManagedResource, OperationRequest
from control_plane.providers import CERTIFICATE_KIND, PROVIDERS, controller_action_policy

from .connections import (
    ConnectionGroup,
    ConnectionLink,
    ConnectionSpec,
    connection_catalog,
)
from .contracts import route_url
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
    # What this node is an instance of -- a provider kind, or the connection
    # family that emitted it. The subtitle already reads as this, but a subtitle
    # is a rendered label and must never become a join key; grouping siblings
    # needs identity.
    kind_key: str = ""
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
    # Fields this declaration asserts that the last observation did not echo
    # back, excluding the ones the provider declared it cannot report. Drift is
    # compared only across fields present in both, so a field the reading omits
    # is unverified rather than agreed -- the difference between "we set this"
    # and "we checked this".
    unconfirmed_fields: tuple[str, ...] = ()
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


_CONNECTION_ROUTES = (
    ("open", "Open connections", "web_route"),
    ("manage", "Manage", "management_route"),
    ("set_up", "Set up", "setup_route"),
)


def _connection_actions(spec: ConnectionSpec) -> tuple[TopologyAction, ...]:
    """Derive a connection's actions from what its spec declared, and no more.

    Every one is a link to a page that enforces its own authorization. The
    effect stays ``read`` because that is all this projection can honestly
    claim: the spec names a destination, not what an operator will do there.
    """

    actions = []
    seen: set[str] = set()
    for name, label, field in _CONNECTION_ROUTES:
        url = route_url(getattr(spec, field))
        # A family whose management page *is* its connections page should offer
        # one button, not the same href twice under two names.
        if not url or url in seen:
            continue
        seen.add(url)
        actions.append(TopologyAction(name, label, "read", url))
    if spec.documentation_url:
        actions.append(
            TopologyAction(
                "documentation", "Documentation", "read", spec.documentation_url
            )
        )
    return tuple(actions)


def _ability_actions(ability, ability_id: str) -> tuple[TopologyAction, ...]:
    """Relate an ability to the graph, and to the capability it names.

    An ability that declares a capability is describing an executable contract
    HQ already owns. Naming it makes the relationship machine-readable without
    the graph acquiring a way to run it.
    """

    actions = [
        TopologyAction("focus", "Show relationships", "read", _focus_url(ability_id))
    ]
    if ability.capability:
        actions.append(
            TopologyAction(
                "command",
                "Open command",
                ability.effect,
                f"{route_url('search')}?{urlencode({'q': ability.capability})}",
                capability=ability.capability,
            )
        )
    return tuple(actions)


def _unconfirmed(resource: ManagedResource, provider) -> tuple[str, ...]:
    """What this declaration asserts that the last reading did not echo back.

    Drift is compared only across fields present in *both*, so a field the
    reading omits is never judged -- it is unverified rather than agreed.
    Fields the provider declared it cannot report are excluded: those are a
    known gap rather than a silent one.
    """

    if not resource.last_observed_at or not isinstance(resource.status, dict):
        return ()
    unobservable = set(getattr(provider, "unobservable_fields", ()) or ())
    return tuple(
        sorted(
            field
            for field in (resource.spec or {})
            if field not in resource.status and field not in unobservable
        )
    )


def _connection_nodes(
    groups: tuple[ConnectionGroup, ...],
    nodes: dict[str, TopologyNode],
    edges: dict[str, TopologyEdge],
) -> None:
    for group in groups:
        spec_actions = _connection_actions(group.spec)
        connection_url = spec_actions[0].url if spec_actions else ""
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
                    actions=_ability_actions(ability, ability_id),
                ),
            )
        for connection in group.connections:
            instance = connection.instance
            connection_id = f"connection:{group.spec.name}:{instance.id}"
            nodes[connection_id] = TopologyNode(
                id=connection_id,
                kind="connection",
                label=instance.label,
                subtitle=instance.kind,
                status=instance.status,
                status_label=instance.status_label,
                detail=instance.detail,
                url=connection_url,
                kind_key=group.spec.name,
                observed_at=(
                    instance.observed_at.isoformat() if instance.observed_at else ""
                ),
                actions=spec_actions,
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
                            (TopologyAction("open", "Open connections", "read", connection_url),)
                            if connection_url
                            else ()
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
            kind_key=resource.kind,
            observed_at=(
                resource.last_observed_at.isoformat()
                if resource.last_observed_at
                else ""
            ),
            declared_revision=resource.generation,
            observed_revision=resource.observed_generation,
            reason=str((resource.conditions or [{}])[0].get("reason", "")).strip(),
            managed=resource.enabled,
            unconfirmed_fields=_unconfirmed(resource, provider),
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


@dataclass(frozen=True)
class TopologyLens:
    """A standing question about the graph, answered from the graph itself.

    A lens owns no inventory and runs no query. It selects ids out of a
    projection already derived and already authorized, so a lens can only ever
    narrow what a principal sees -- never widen it.
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
        if not node.observed_at or not node.kind_key:
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
    TopologyLens("unobserved-resources", "Resources no connection reports",
        "Declared resources that no live connection currently names as a dependency.",
        _unobserved_resources),
    TopologyLens("ungoverned-resources", "Resources no ability governs",
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


def serialize_topology(
    topology: Topology, *, lens: TopologyLens | None = None
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for node in topology.nodes:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    return {
        "ok": True,
        "schema_version": 1,
        # Which lens produced this payload, and every lens that could have.
        "lens": lens.name if lens else None,
        "lenses": [
            {"name": item.name, "label": item.label, "summary": item.summary}
            for item in TOPOLOGY_LENSES
        ],
        "summary": {
            "nodes": len(topology.nodes),
            "edges": len(topology.edges),
            "kinds": dict(sorted(counts.items())),
        },
        "nodes": [asdict(node) for node in topology.nodes],
        "edges": [asdict(edge) for edge in topology.edges],
    }


def topology(*, principal: Principal, lens: str = "") -> dict[str, Any]:
    """Return the shared serialized projection for machine delivery adapters."""

    selected = lens_for(lens) if lens else None
    projection = derive_topology(principal=principal)
    if selected is not None:
        projection = apply_lens(projection, selected)
    return serialize_topology(projection, lens=selected)
