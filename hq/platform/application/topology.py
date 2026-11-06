"""A live infrastructure topology derived from HQ's canonical contracts.

Nothing in this module is persisted. Connections are observations, resources
are declarations, and abilities come from the provider registry; this merely
joins them into nodes and edges for every delivery adapter. Manipulation is a
link to an existing application capability or web use case, never a graph-only
mutation that could drift from the thing it claims to represent.
"""

import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, create_model

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.application.routes import reverse
lazy from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

from .action_links import (
    READ_NOW_CAPABILITY,
    ActionLink as TopologyAction,
    capability_action_link,
    command_action,
    command_url,
    connection_action_links,
    topology_url,
)
from .analytics import HOST_TRAFFIC_DAYS, traffic_for_hosts
from .connection_catalog import ConnectionGroup, connection_catalog
from .connections import ConnectionLink, ConnectionSpec
from .contradiction_findings import add_contradiction_facts
from .derivations import derivation
from .derived_inputs import ESTATE_READS, estate_variant
from .entity_links import entity_link, kind_label, node_name, record_name
from .infrastructure import RESOURCE_TONES, is_drifted, resource_health
from .integration_validation import required_capability_names
from .projection import QUERY, QueryRefusal, projection_scope
from .resource_capabilities import REMOVAL_CAPABILITY, removals_pending, resource_capabilities
from .resource_context import record_summary, shared_names
from .security import Capability, Principal
from .topology_answer import TOPOLOGY_SCHEMA_VERSION, TopologyAnswer, TopologySummary
from .topology_facts import add_connection_facts, add_observed_facts
from .topology_lenses import (
    MAX_TRACE_DEPTH,
    TOPOLOGY_LENSES,
    TRACE_DIRECTIONS,
    TopologyLens,
    apply_lens,
    apply_trace,
    lens_for,
)
from .topology_model import (
    NODE_KIND_ORDER,
    Topology,
    TopologyEdge,
    TopologyNode,
    TopologyTrace,
    derived_id,
    edge_between,
    gate_covers,
    newest_stamp,
)
lazy from .adoption import manages_through
lazy from .facts import stored_snapshots
lazy from .hq_self import serving
lazy from .posture import estate_posture
lazy from .topology_estate import add_estate


def _focus_url(node_id: str) -> str:
    return topology_url(node_id)


def _resource_status(resource: ManagedResource) -> tuple[str, str, str]:
    if not resource.enabled:
        return "neutral", "Switched off in HQ", ""
    health = resource_health(resource)

    state = RESOURCE_TONES.get(health["state"], "neutral")
    return state, health["label"], health["message"]


def _resource_actions(
    resource: ManagedResource, principal: Principal, *, removal_pending: bool, manages
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
        command_action(
            "edit",
            "infrastructure.resource.update",
            reverse("control_plane:edit", kwargs={"key": key}),
            target=key,
        )
    )
    capabilities = resource_capabilities(resource, running=(), removal_pending=removal_pending, manages=manages)
    drifted = is_drifted(resource)
    if drifted:
        # Something changed it outside HQ: keeping that is the choice that
        # loses nothing, so it is offered, and first.
        actions.append(
            command_action(
                "keep_live",
                "infrastructure.resource.accept_observed",
                command_url("infrastructure.resource.accept_observed", key),
                target=key,
            )
        )
    reconcile = capabilities.actions.get("reconcile")
    if reconcile and reconcile.enabled:
        actions.append(
            command_action(
                "reconcile",
                "infrastructure.resource.reconcile",
                reverse("control_plane:reconcile", kwargs={"key": key}),
                method="POST",
                target=key,
            )
        )
    renew = capabilities.actions.get("renew")
    if renew and renew.enabled and principal.permits(Capability.REQUEST_CERTIFICATE_RENEWAL):
        actions.append(
            command_action(
                "renew",
                "certificate.renew",
                reverse("control_plane:renew", kwargs={"key": key}),
                method="POST",
                target=key,
            )
        )
    if capabilities.removal != "unavailable":
        actions.append(
            command_action(
                "remove",
                REMOVAL_CAPABILITY,
                reverse("control_plane:remove", kwargs={"key": key}),
                target=key,
                intent=capabilities.removal_words,
            )
        )
    return tuple(actions)


def _link_node(
    link: ConnectionLink, *, kind: str, read_capabilities: tuple[str, ...], through: str = ""
) -> TopologyNode:
    """A node for something a connection names and HQ keeps no record of.

    A link carries a name and a page, and no noun for what the thing is. A
    target says which connection reaches it. Nothing reads a dependency.
    """

    node_id = derived_id(kind, link.url, link.label)
    reached = f"Reached through {through}" if through else "Reached by a connection"
    return TopologyNode(
        id=node_id,
        kind=kind,
        label=link.label,
        subtitle=reached if kind == "target" else "Not read yet",
        url=link.url,
        actions=((TopologyAction("open", "Open", "read", link.url),) if link.url else ()),
        read_capabilities=read_capabilities,
    )


def _named_by(nodes: dict[str, TopologyNode], node: TopologyNode) -> None:
    """Add a node a connection names, or widen who may read one another already named."""

    current = nodes.get(node.id)
    nodes[node.id] = (
        node
        if current is None
        else replace(current, read_capabilities=_least_gate(current.read_capabilities, node.read_capabilities))
    )


def _least_gate(current: tuple[str, ...], named: tuple[str, ...]) -> tuple[str, ...]:
    """The gate of a node more than one family names: a reader of either may be
    told of it, so the narrower gate where one holds the other, else both,
    since one gate cannot say either."""

    if gate_covers(named, current):
        return current
    if gate_covers(current, named):
        return named
    return tuple(dict.fromkeys((*current, *named)))


def _connection_actions(spec: ConnectionSpec) -> tuple[TopologyAction, ...]:
    """Derive a connection's actions from what its spec declared, and no more.

    Every one is a link to a page that enforces its own authorization. The
    effect stays ``read`` because that is all this projection can honestly
    claim: the spec names a destination, not what an operator will do there.
    """

    return connection_action_links(spec)


def _ability_actions(ability, ability_id: str, principal: Principal) -> tuple[TopologyAction, ...]:
    """Relate an ability to the graph, and to the capability it names.

    An ability that declares a capability is describing an executable contract
    HQ already owns. Naming it makes the relationship machine-readable without
    the graph acquiring a way to run it.
    """

    actions = [TopologyAction("focus", "Show links", "read", _focus_url(ability_id))]
    command = capability_action_link(ability.capability, principal=principal)
    if command is not None:
        actions.append(command)
    return tuple(actions)


def _asserts_nothing(value: Any) -> bool:
    """Whether a declared value is the absence of a claim rather than a claim.

    A spec is a full model dump, so a TXT record carries a ``priority`` key
    because MX records exist; ``None`` there is the schema showing through.
    ``False`` and ``0`` are claims: an MX at priority zero said something.
    """

    return value is None or value in ("", [], {})


def _unconfirmed(resource: ManagedResource, provider) -> tuple[str, ...]:
    """What this declaration asserts that the last reading did not echo back.

    Drift compares only fields present in both, so a field the reading omits is
    unverified rather than agreed. Fields the provider declared it cannot
    report, and fields carrying no value, have nothing to confirm.
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
    unobservable = set(provider.unobservable_fields if provider is not None else ())
    # Without the empty-value clause every DNS record but an MX would assert an
    # unconfirmed ``priority`` nothing can clear, since the provider rightly
    # reads none back for a type that has none.
    return tuple(
        sorted(
            field
            for field, value in (resource.spec or {}).items()
            if field not in resource.status and field not in unobservable and not _asserts_nothing(value)
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
    read_capabilities: tuple[str, ...],
    observed_at: str = "",
) -> None:
    """Join every distinct emitted connection workflow onto one controller.

    Its observed time is the newest of the connections it reports.
    """

    action = TopologyAction("open", f"Open {group_label}", "read", url) if url else None
    refresh = capability_action_link(READ_NOW_CAPABILITY, principal=principal)
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
            read_capabilities=read_capabilities,
        )
    else:
        additions = tuple(item for item in emitted if all(existing.url != item.url for existing in current.actions))
        nodes[node_id] = replace(
            current,
            observed_at=newest_stamp(current.observed_at, observed_at),
            actions=current.actions + additions,
            read_capabilities=_least_gate(current.read_capabilities, read_capabilities),
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
            _target_edges(instance, node, nodes, edges)
            _dependency_edges(instance, node, nodes, edges)
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
                subtitle=group.spec.label,
                detail=ability.summary,
                url=_focus_url(ability_id),
                actions=_ability_actions(ability, ability_id, principal),
                read_capabilities=required_capability_names(group.spec),
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
        read_capabilities=required_capability_names(group.spec),
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
        read_capabilities=node.read_capabilities,
        observed_at=node.observed_at,
    )
    relation = edge_between(controller_id, node.id, "carries")
    edges[relation.id] = relation


def _target_edges(instance, connection, nodes, edges) -> None:
    connection_id = connection.id
    for target in instance.targets:
        node = _link_node(target, kind="target", read_capabilities=connection.read_capabilities, through=instance.label)
        _named_by(nodes, node)
        relation = edge_between(connection_id, node.id, "reaches", status=instance.status)
        edges[relation.id] = relation
        # A target that is also a declaration using this connection.
        resource_id = f"resource:{target.resource_key}"
        if target.resource_key and resource_id in nodes:
            relation = edge_between(connection_id, resource_id, "used_by", status=instance.status)
            edges[relation.id] = relation


def _dependency_edges(instance, connection, nodes, edges) -> None:
    connection_id = connection.id
    for dependency in instance.dependencies:
        resource_id = f"resource:{dependency.resource_key}"
        if dependency.resource_key and resource_id in nodes:
            target_id = resource_id
        else:
            node = _link_node(dependency, kind="dependency", read_capabilities=connection.read_capabilities)
            _named_by(nodes, node)
            target_id = node.id
        relation = edge_between(connection_id, target_id, "used_by", status=instance.status)
        edges[relation.id] = relation


def _ability_edges(group, connection, connection_id, edges) -> None:
    for state in connection.abilities:
        ability_id = f"ability:{group.spec.name}:{state.ability.name}"
        available = "good" if state.available is True else "serious" if state.available is False else "neutral"
        relation = edge_between(connection_id, ability_id, "enables", status=available)
        edges[relation.id] = relation


# A label is a candidate hostname when it looks like one. Deliberately a shape
# test rather than a list of kinds: a target is a hostname, a resource key
# sometimes is, and an extension may emit a node kind this module has never
# heard of. Asking what the label *is* keeps that open.
_HOSTNAME_SHAPE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+\.?$")


def _measure(nodes: dict[str, TopologyNode]) -> None:
    """Give every node named like a host what that host actually served.

    Joined by name, as the service page joins: analytics stores a reading
    against a hostname and these nodes *are* hostnames, so no key ties them.
    It sits in the shared projection because a graph measuring nothing pays
    nothing.
    """

    # One query for the whole graph, and none when nothing is named like a host.
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

    principal.require(Capability.READ)
    with projection_scope(seed=serving(request) if request is not None else None):
        return _derive(principal)


def _resource_node(resource: ManagedResource) -> TopologyNode:
    """A declaration as a node: its name, its kind's label, its page.

    A container is named as itself, with its machine in the subtitle: its
    key joins the two, and printed as the name it would repeat the machine
    wherever the machine is already the context.
    """

    name = str((resource.spec or {}).get("name", "") or "") if resource.kind == CONTAINER_KIND else ""
    host = str((resource.spec or {}).get("host", "") or "") if name else ""
    label = name or resource.key
    shown = record_name(resource.kind, resource.spec or {}, resource.key)
    return TopologyNode(
        id=f"resource:{resource.key}",
        kind="resource",
        label=label,
        name=shown if shown != label else "",
        subtitle=f"{kind_label(resource.kind)} on {host}" if host else kind_label(resource.kind),
        url=resource.get_absolute_url(),
        kind_key=resource.kind,
    )


@dataclass(frozen=True, slots=True)
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
        return RelationGraph(Topology(tuple(nodes.values()), tuple(edges.values())), subjects)

    return _relations(principal, build)


@derivation(
    "estate.relations",
    reads=ESTATE_READS,
    vary=lambda principal, _build: estate_variant(principal),
)
def _relations(_principal: Principal, build: Any) -> RelationGraph:
    return build()


@derivation("estate.topology", reads=ESTATE_READS, vary=estate_variant)
def _derive(principal: Principal) -> Topology:
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
            key=lambda node: (NODE_KIND_ORDER.get(node.kind, 99), node.label.casefold(), node.id),
        )
    )
    ordered_edges = tuple(sorted(edges.values(), key=lambda edge: (edge.kind, edge.source, edge.target)))
    unmet = tuple(
        (standing.check.id, standing.reading.evidence, standing.reading.read_capabilities)
        for standing in estate_posture(groups).unmet
    )
    return Topology(ordered_nodes, ordered_edges, unmet, _kinds_read())


def _kinds_read() -> tuple[tuple[str, datetime], ...]:
    """When each kind was last read with an answer, from its stored snapshots."""

    return tuple(
        (kind, max(read))
        for kind, rows in sorted(stored_snapshots().items())
        if (read := [row.observed_at for row in rows if row.observed_at is not None])
    )


def _resource_nodes(resources, principal: Principal) -> dict[str, TopologyNode]:
    """One node per declaration, with its state and what may be done to it."""

    # Only an operator is offered actions, so only an operator's view reads this.
    pending_removal = (
        removals_pending() if resources and principal.permits(Capability.MANAGE_INFRASTRUCTURE) else frozenset()
    )
    # Read on first use, once for every resource.
    manages = manages_through()
    named = {resource.key: _resource_node(resource) for resource in resources}
    shared = shared_names(node_name(node) for node in named.values())
    nodes: dict[str, TopologyNode] = {}
    for resource in resources:
        provider = PROVIDERS.get(resource.kind)
        status, status_label, detail = _resource_status(resource)
        node = named[resource.key]
        nodes[node.id] = replace(
            node,
            summary=record_summary(resource, node_name(node)) if node_name(node) in shared else "",
            status=status,
            status_label=status_label,
            detail=detail,
            observed_at=(resource.last_observed_at.isoformat() if resource.last_observed_at else ""),
            declared_revision=resource.generation,
            observed_revision=resource.observed_generation,
            reason=str((resource.conditions or [{}])[0].get("reason", "")).strip(),
            managed=resource.enabled,
            on_demand=bool((resource.spec or {}).get("on_demand")),
            unconfirmed_fields=_unconfirmed(resource, provider),
            actions=_resource_actions(
                resource, principal, removal_pending=resource.key in pending_removal, manages=manages
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
                    relation = edge_between(ability_id, resource_id, "governs")
                    edges[relation.id] = relation


class InvalidTopologyQuery(QueryRefusal):
    """A focus the topology holds no node for; ``reason`` says where ids come from."""


# Built from the declarations, so a type checker reads each as a value.
_LENS: Any = Literal[tuple(lens.name for lens in TOPOLOGY_LENSES)]
_DIRECTION: Any = Literal[TRACE_DIRECTIONS]

# The API's query and get_topology's arguments are both this model, so the
# two doors publish one schema and refuse one input in one sentence.
TopologyQuery = create_model(
    "TopologyQuery",
    __config__=QUERY,
    __doc__="What a topology read is narrowed by.",
    lens=(_LENS | None, Field(None, description="A question the graph is narrowed to, such as what has a problem.")),
    focus=(
        Annotated[str, StringConstraints(strip_whitespace=True)],
        Field("", description="A node's id, whose neighborhood a trace follows."),
    ),
    direction=(_DIRECTION, Field("both", description="Which way a trace from focus follows the edges.")),
    depth=(int, Field(2, ge=1, le=MAX_TRACE_DEPTH, description="How many hops a trace from focus follows.")),
)


def _counts(topology: Topology) -> TopologySummary:
    kinds = Counter(node.kind for node in topology.nodes)
    return {"nodes": len(topology.nodes), "edges": len(topology.edges), "kinds": dict(sorted(kinds.items()))}


def serialize_topology(
    topology: Topology,
    *,
    lens: TopologyLens | None = None,
    trace: TopologyTrace | None = None,
) -> TopologyAnswer:
    return {
        "schema_version": TOPOLOGY_SCHEMA_VERSION,
        "lens": lens.name if lens else None,
        "trace": (
            {
                "focus": trace.focus,
                "direction": trace.direction,
                "depth": trace.depth,
                "hops": [{"node": node_id, "hop": hop} for node_id, hop in trace.hops],
            }
            if trace
            else None
        ),
        "summary": _counts(topology),
        "nodes": [asdict(node) for node in topology.nodes],
        "edges": [asdict(edge) for edge in topology.edges],
    }


def topology(*, principal: Principal, **narrowing: object) -> TopologyAnswer:
    """The shared serialized projection for machine delivery adapters.

    Nodes come with a lens or a focus. Asked for neither, it answers what the
    estate holds and the lenses that would narrow it, since the whole graph is
    more than a client can read. ``narrowing`` is judged by ``TopologyQuery``
    once the caller may read, and one that names nothing is refused with the
    choices that would.
    """

    principal.require(Capability.READ)
    query = TopologyQuery.model_validate(narrowing).model_dump()
    selected: TopologyLens | None = lens_for(query["lens"]) if query["lens"] else None
    focus: str = query["focus"]
    projection = derive_topology(principal=principal)
    if not selected and not focus:
        return {
            "schema_version": TOPOLOGY_SCHEMA_VERSION,
            "lens": None,
            "trace": None,
            "summary": _counts(projection),
            "lenses": [{"name": item.name, "label": item.label, "summary": item.summary} for item in TOPOLOGY_LENSES],
        }
    if selected is not None:
        projection = apply_lens(projection, selected)
    if focus and focus not in {node.id for node in projection.nodes}:
        raise InvalidTopologyQuery(
            "topology: focus names no node here; it is a node's id, as a lens's answer gives each one."
        )
    projection, trace = apply_trace(projection, focus, direction=query["direction"], depth=query["depth"])
    return serialize_topology(projection, lens=selected, trace=trace)
