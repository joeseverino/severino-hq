"""Machines, services and domains as nodes of their own, and the readings between them.

The estate is the hub of the topology. A controller or a reached target that
names a machine, a service or a domain folds into that node. Every stored
reading joined to an estate node through ``application.facts`` becomes an edge
from the connection that read it, labelled with the reading's relation phrase,
and exists only while the reading does.

Resolution is the machine catalogue's own index, the service catalogue and the
zone inventory: never a match on labels alone. What an extension reaches that
is none of these stays a target.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any


from control_plane.providers import CONTAINER_KIND, PROVIDERS, normalized_hostname

from .action_links import ActionLink as TopologyAction
from .entity_links import entity_link
from .facts import Joined, Subject, inventory_records, readings
from .locate import Machines, index_of
from .connections import machines_once
from .topology import TopologyEdge, TopologyNode, _derived_id, _edge, newest_stamp

ESTATE_KINDS = ("machine", "service", "zone")

# Derived node kinds that only stand for a machine something mentioned: a
# controller is the machine it runs on, a target is the machine it reaches.
_FOLDS_INTO_MACHINE = {"controller": "Runs the controller", "target": "Reached as"}

_SERVICE_STATUS = {"good": "good", "attention": "attention", "serious": "serious"}


@dataclass
class _Estate:
    subjects: dict[str, Subject]
    machine_ids: dict[str, str]
    index: Machines
    # When a reading or an inventory record describing each node was taken.
    seen: dict[str, list[str]] = field(default_factory=dict)

    def saw(self, node_id: str, *moments: Any) -> None:
        """Record when something describing ``node_id`` was read."""

        stamps = self.seen.setdefault(node_id, [])
        for moment in moments:
            if isinstance(moment, datetime):
                stamps.append(moment.isoformat())
            elif moment:
                stamps.append(str(moment))

    def machine(self, value: Any) -> str:
        """The machine node a name or address stands for, or ""."""

        text = str(value or "").strip()
        if not text:
            return ""
        return self.machine_ids.get(text) or self.machine_ids.get(
            self.index.resolve(text), ""
        )


def add_estate(
    nodes: dict[str, TopologyNode],
    edges: dict[str, TopologyEdge],
    resources: tuple[Any, ...],
) -> dict[str, Subject]:
    """Add estate nodes, fold what names them, and draw reading edges.

    Returns each estate node's join keys, by node id.
    """

    estate = _machines(nodes, edges, resources)
    zones = _zones(nodes, edges, resources, estate)
    _services(nodes, edges, estate, zones)
    _fold(nodes, edges, estate)
    _unrecognised_containers(nodes, estate)
    _hosted(nodes, edges, resources, estate)
    _reading_edges(nodes, edges, estate)
    _observed(nodes, edges, estate)
    return dict(estate.subjects)


def _observed(nodes, edges, estate: _Estate) -> None:
    """Each estate node's newest reading, record or declaration observation."""

    for edge in edges.values():
        if edge.kind == "declared_by" and edge.target in nodes:
            estate.saw(edge.source, nodes[edge.target].observed_at)
    for node_id, stamps in estate.seen.items():
        node = nodes.get(node_id)
        if node is None:
            continue
        stamp = newest_stamp(node.observed_at, *stamps)
        if stamp != node.observed_at:
            nodes[node_id] = replace(node, observed_at=stamp)


def _open(url: str) -> tuple[TopologyAction, ...]:
    return (TopologyAction("open", "Open", "read", url),) if url else ()


def _declared_by(edges, node_id: str, resource_id: str, nodes) -> None:
    if resource_id in nodes:
        relation = _edge(node_id, resource_id, "declared_by", "Declared by")
        edges[relation.id] = relation


def _machines(nodes, edges, resources) -> _Estate:
    catalog = machines_once()
    connections: dict[str, list[str]] = {}
    for node in nodes.values():
        if node.kind == "connection":
            connections.setdefault(node.connection_ref, []).append(node.id)
    subjects: dict[str, Subject] = {}
    machine_ids: dict[str, str] = {}
    for machine in catalog:
        node_id = f"machine:{machine.name}"
        nodes[node_id] = TopologyNode(
            id=node_id,
            kind="machine",
            label=machine.name,
            subtitle=machine.role or "Machine",
            status="good" if machine.reachable else "neutral",
            status_label="Reachable" if machine.reachable else "",
            url=machine.url,
            kind_key="machine",
            actions=_open(machine.url),
        )
        for name in (machine.name, *machine.aliases):
            machine_ids.setdefault(name, node_id)
        if machine.declaration:
            _declared_by(edges, node_id, f"resource:{machine.declaration}", nodes)
        # The connections the catalogue says reach it, the same answer as the
        # machine list's "Reached through".
        for ref in machine.reached_by:
            for connection_id in connections.get(ref, ()):
                relation = _edge(connection_id, node_id, "reaches")
                edges[relation.id] = relation
        subjects[node_id] = Subject.of(
            hostnames=(machine.name, *machine.aliases), addresses=machine.addresses
        )
    index = index_of(
        declared=[{"name": item.name, "addresses": item.addresses} for item in catalog]
    )
    estate = _Estate(subjects=subjects, machine_ids=machine_ids, index=index)
    # Its device reading, its telemetry and its containers.
    for machine in catalog:
        estate.saw(
            f"machine:{machine.name}",
            getattr(machine.presence, "observed_at", None),
            machine.telemetry_observed_at,
            *(item.observed_at for item in machine.containers),
        )
    return estate


def _zones(nodes, edges, resources, estate: _Estate) -> tuple[str, ...]:
    from .zones import ZONE_KIND, zone_reads

    names, read = zone_reads(resources)
    for name in names:
        estate.saw(f"zone:{name}", *read.get(name, ()))
        node_id = f"zone:{name}"
        url = entity_link("zone", name).url
        nodes[node_id] = TopologyNode(
            id=node_id,
            kind="zone",
            label=name,
            subtitle="Domain",
            url=url,
            kind_key="zone",
            actions=_open(url),
        )
        # Every name under the domain: its Access applications and edge
        # certificates are the domain's as much as any one service's.
        estate.subjects[node_id] = Subject.of(zones=(name,))
    for resource in resources:
        if resource.kind != ZONE_KIND:
            continue
        name = normalized_hostname(str((resource.spec or {}).get("zone", "")))
        if name:
            _declared_by(edges, f"zone:{name}", f"resource:{resource.key}", nodes)
    return names


def _services(nodes, edges, estate: _Estate, zones: tuple[str, ...]) -> None:
    from .services import service_catalog, zone_holding

    # Longest first, so a name joins the most specific domain holding it.
    by_length = sorted(zones, key=len, reverse=True)
    for service in service_catalog():
        node_id = f"service:{service.hostname}"
        nodes[node_id] = TopologyNode(
            id=node_id,
            kind="service",
            label=service.hostname,
            subtitle="Service",
            status=_SERVICE_STATUS.get(service.status, "neutral"),
            status_label=service.status_label,
            detail=service.faults[0] if service.faults else "",
            url=service.url,
            kind_key="service",
            actions=_open(service.url),
        )
        estate.subjects[node_id] = Subject.of(
            hostnames=(service.hostname, *service.aliases)
        )
        estate.saw(
            node_id,
            *(facet.observed.observed_at for facet in service.facets if facet.observed),
        )
        for claim in service.declared_claims:
            _declared_by(edges, node_id, f"resource:{claim.resource_key}", nodes)
        if service.origin is not None and service.origin.host:
            machine = estate.machine(service.origin.host)
            if machine:
                relation = _edge(node_id, machine, "runs_on", "Runs on")
                edges[relation.id] = relation
        zone = zone_holding(service.hostname, zones)
        if zone:
            relation = _edge(f"zone:{zone}", node_id, "contains", "Contains")
            edges[relation.id] = relation
    _hq_service(nodes, edges, estate, by_length)


def _hq_service(nodes, edges, estate: _Estate, zones: list[str]) -> None:
    """HQ's own service, on the machine the catalogue says runs it. Read-only."""

    from .hq_self import hq_service
    from .services import zone_holding

    own = hq_service(catalog=machines_once())
    if own is None:
        return
    node_id = f"service:{own.hostname}"
    if node_id not in nodes:
        url = entity_link("service", own.hostname).url
        nodes[node_id] = TopologyNode(
            id=node_id,
            kind="service",
            label=own.hostname,
            subtitle=own.label,
            detail="Read-only",
            url=url,
            kind_key="service",
            actions=_open(url),
        )
        estate.subjects[node_id] = Subject.of(hostnames=own.hostnames)
    machine = estate.machine(own.machine)
    if machine:
        relation = _edge(node_id, machine, "runs_on", "Runs on")
        edges[relation.id] = relation
    zone = zone_holding(own.hostname, zones)
    if zone:
        relation = _edge(f"zone:{zone}", node_id, "contains", "Contains")
        edges[relation.id] = relation


def _fold(nodes, edges, estate: _Estate) -> None:
    """Move controllers and targets that name an estate node onto it."""

    folded: dict[str, str] = {}
    for node_id, node in nodes.items():
        if node.kind not in _FOLDS_INTO_MACHINE:
            continue
        machine = estate.machine(node.label)
        if machine:
            folded[node_id] = machine
            continue
        if node.kind != "target":
            continue
        name = normalized_hostname(node.label)
        for candidate in (f"service:{name}", f"zone:{name}"):
            if candidate in nodes:
                folded[node_id] = candidate
                break
    for node_id, into in folded.items():
        node, host = nodes[node_id], nodes[into]
        estate.saw(into, node.observed_at)
        facts = host.facts
        if host.kind == "machine":
            fact = (_FOLDS_INTO_MACHINE[node.kind], node.label)
            facts = facts + ((fact,) if fact not in facts else ())
        nodes[into] = replace(
            host,
            facts=facts,
            actions=host.actions
            + tuple(
                action
                for action in node.actions
                if all(existing.url != action.url for existing in host.actions)
            ),
        )
    if not folded:
        return
    moved: dict[str, TopologyEdge] = {}
    for edge in edges.values():
        source = folded.get(edge.source, edge.source)
        target = folded.get(edge.target, edge.target)
        if source == target:
            continue
        relation = replace(
            edge,
            id=_derived_id("edge", source, target, edge.kind),
            source=source,
            target=target,
        )
        moved[relation.id] = relation
    edges.clear()
    edges.update(moved)
    for node_id in folded:
        del nodes[node_id]


def _unrecognised_containers(nodes, estate: _Estate) -> None:
    """A container on a machine that no compose project declares, as its fact.

    The sweep did not adopt it, so it has no node of its own; its machine
    carries it, and a finding reads it from there.
    """

    from .inventory import unmanaged

    if not estate.machine_ids:
        return
    for item in unmanaged():
        if item.adoptable or item.kind != CONTAINER_KIND:
            continue
        host = str(item.spec.get("host", ""))
        machine_id = estate.machine(host)
        if machine_id:
            fact = ("unrecognised-container", f"{item.spec.get('name', '')}@{host}")
            machine = nodes[machine_id]
            if fact not in machine.facts:
                nodes[machine_id] = replace(machine, facts=machine.facts + (fact,))


def _hosted(nodes, edges, resources, estate: _Estate) -> None:
    """What declares the machine it runs on, and a machine's tailnet device."""

    _runs_edges(nodes, edges, resources, estate)
    _tailnet_edges(nodes, edges, resources, estate)


def _runs_edges(nodes, edges, resources, estate: _Estate) -> None:
    from .machines import declares_host

    for resource in resources:
        resource_id = f"resource:{resource.key}"
        if resource_id not in nodes or not declares_host(resource.kind):
            continue
        host = estate.machine((resource.spec or {}).get("host"))
        if host:
            relation = _edge(host, resource_id, "runs", "Runs")
            edges[relation.id] = relation


def _tailnet_edges(nodes, edges, resources, estate: _Estate) -> None:
    from . import tailnet

    devices = None
    read_through: dict[str, str] | None = None
    for resource in resources:
        device_id = f"resource:{resource.key}"
        if resource.kind != tailnet.TAILNET_KIND or device_id not in nodes:
            continue
        devices = tailnet.devices() if devices is None else devices
        name = str((resource.spec or {}).get("name") or "")
        for host in _device_hosts(devices.get(name), estate):
            relation = _edge(host, device_id, "on_tailnet", "On the tailnet as")
            edges[relation.id] = relation
        # The connection that reads the device: the record's own, else the
        # tailnet connections, as the machine catalogue decides it.
        if read_through is None:
            read_through = {
                str(record.get("name", "")): str(record.get("connection_ref", "") or "")
                for _snapshot, record in inventory_records(tailnet.TAILNET_KIND)
            }
        if name in read_through:
            _device_reader_edges(nodes, edges, device_id, read_through[name])


def _device_hosts(device, estate: _Estate) -> set[str]:
    """The machines holding any of a tailnet device's addresses."""

    return {
        estate.machine_ids.get(estate.index.at(address), "")
        for address in (device.addresses if device else ())
    } - {""}


def _device_reader_edges(nodes, edges, device_id: str, ref: str) -> None:
    from . import tailnet

    for node in nodes.values():
        if node.kind != "connection":
            continue
        if node.connection_ref == ref or (
            not ref and node.provider in PROVIDERS[tailnet.TAILNET_KIND].connection_providers
        ):
            relation = _edge(node.id, device_id, "used_by")
            edges[relation.id] = relation


def _reading_edges(nodes, edges, estate: _Estate) -> None:
    """One edge per reading kind from the connection that read it to its subject."""

    by_ref: dict[str, list[str]] = {}
    by_provider: dict[str, list[str]] = {}
    for node in nodes.values():
        if node.kind != "connection":
            continue
        by_ref.setdefault(node.connection_ref, []).append(node.id)
        by_provider.setdefault(node.provider, []).append(node.id)
    index = readings()
    grouped: dict[tuple[str, str, str], list[Joined]] = {}
    for node_id, subject in estate.subjects.items():
        for joined in index.about(subject):
            for source in _readers(joined, by_ref, by_provider, nodes):
                grouped.setdefault((source, node_id, joined.kind), []).append(joined)
    for (source, target, kind), items in grouped.items():
        moments = [item.observed_at for item in items if item.observed_at]
        oldest = min(moments, default=None)
        estate.saw(target, *moments)
        # A registry, or a connection no controller reports, is seen only
        # through what it read.
        if not nodes[source].observed_at:
            estate.saw(source, *moments)
        titles = tuple(dict.fromkeys(item.title for item in items if item.title))
        entities = tuple(
            dict.fromkeys(entity_link(item.kind, "", record=item.record) for item in items)
        )
        relation = TopologyEdge(
            id=_derived_id("edge", source, target, kind),
            source=source,
            target=target,
            kind="reading",
            label=items[0].relation,
            status="attention" if any(item.stale for item in items) else "neutral",
            source_kind=kind,
            detail=", ".join(titles),
            observed_at=oldest.isoformat() if oldest else "",
            entities=entities,
            facet=items[0].facet,
        )
        edges[relation.id] = relation


def _readers(joined: Joined, by_ref, by_provider, nodes) -> tuple[str, ...]:
    """The nodes that took this reading.

    A reading HQ takes itself comes from its public registry's node. Otherwise
    the record's ``connection_ref`` where it names one; otherwise the
    connections of the reading's provider, narrowed to the controller that
    stored it.
    """

    if joined.spec.read_by == "hq":
        node_id = f"registry:{joined.spec.provider}"
        nodes.setdefault(
            node_id,
            TopologyNode(
                id=node_id,
                kind="registry",
                label=joined.spec.provider.upper(),
                subtitle="Public registry",
                kind_key="registry",
            ),
        )
        return (node_id,)
    if joined.connection_ref and joined.connection_ref in by_ref:
        return tuple(by_ref[joined.connection_ref])
    found = by_provider.get(joined.spec.provider, [])
    if joined.controller_id:
        narrowed = [
            node_id for node_id in found if nodes[node_id].controller_id == joined.controller_id
        ]
        found = narrowed or found
    if found:
        return tuple(found)
    # A stored reading whose connection no controller reports now: the edge
    # stays, from a node naming that connection, rather than vanishing.
    name = joined.connection_ref or joined.spec.provider
    node_id = f"connection:unreported:{name}"
    nodes.setdefault(
        node_id,
        TopologyNode(
            id=node_id,
            kind="connection",
            label=name,
            subtitle=joined.spec.provider,
            status_label="Not reported",
            detail="No controller reports this connection now.",
            kind_key=joined.spec.provider,
        ),
    )
    by_ref.setdefault(name, []).append(node_id)
    return (node_id,)
