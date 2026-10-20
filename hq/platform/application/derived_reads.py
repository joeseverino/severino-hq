"""What the estate pages derive, as registered read resources.

Each handler asks the function its page asks and projects the answer to JSON.
Nothing here derives a fact: the estate reading, the machine and service
catalogues, the relation graph, the join engine, credential sight and the
action queue do. Readings leave only through their schema
(``ObservationSpec.admitted``); no provider payload and no secret is stored,
so none can be returned.
"""

from dataclasses import asdict
from datetime import datetime
from typing import Any

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.names import is_hostname, normalized_hostname
from hq.domains.control_plane.observations import OBSERVATIONS

from .labels import human_label
from .own_addresses import tailnet_sightings
from .projection import page_size, projection_scope
from .record_errors import RecordNotFound
from .security import Principal
lazy from . import connections, dashboard, estate, machines, request_context
lazy from .action_items import filter_items
lazy from .command_center import estate_search
lazy from .credential_sight import credential_sight, sight
lazy from .entity_links import entity_link
lazy from .estate import estate_reading
lazy from .hq_self import serving
lazy from .infrastructure import enabled_resources
lazy from .machine_context import header_addresses
lazy from .paths import path_to
lazy from .relationships import readout_records, relationships_for
lazy from .request_path import request_path, serialize_request_path
lazy from .search import global_search
lazy from .service_list import services_by_zone
lazy from .topology import relation_graph
lazy from .zones import ZONE_KIND, domain_context


def _moment(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _link(link: Any) -> dict[str, Any] | None:
    if link is None:
        return None
    return {
        "label": link.label,
        "url": link.url,
        "external": link.external,
        "kind": link.kind,
        "kind_label": link.kind_label,
    }


def _collection(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


# ----- Estate -----------------------------------------------------------------


def list_estate() -> dict[str, Any]:
    """The dashboard's estate figures, each linking to the page that lists it."""

    with projection_scope():
        return _collection(
            [
                {
                    "id": card["id"],
                    "label": card["label"],
                    "value": card["value"],
                    "detail": card.get("detail", ""),
                    "status": card.get("status", ""),
                    "url": card["url"],
                }
                for card in estate.cards()
            ]
        )


def list_action_items(*, query: str = "", status: str = "", source: str = "", limit: int = 50) -> dict[str, Any]:
    """The composed action queue, filtered the way the action items page filters it."""

    with projection_scope():
        items = filter_items(dashboard.work_queue(), query=query, status=status, source=source)
    return _collection(items[: page_size(limit)])


# ----- Machines ---------------------------------------------------------------


def _presence(presence: Any) -> dict[str, Any] | None:
    if presence is None:
        return None
    return {
        "online": presence.online,
        "last_seen": presence.last_seen or None,
        "tailnet_name": presence.tailnet_name,
        "dns_name": presence.dns_name,
        "tailnet_address": presence.tailnet_address,
        "lan_address": tailnet_sightings().lan_address(presence),
        "os": presence.os,
        "client_version": presence.client_version,
        "update_available": presence.update_available,
        "key_expires": presence.key_expires or None,
        "authorized": presence.authorized,
        "tags": list(presence.tags),
        "advertised_routes": list(presence.advertised_routes),
        "enabled_routes": list(presence.enabled_routes),
        "offers_exit_node": presence.offers_exit_node,
        "exit_node_approved": presence.exit_node_approved,
        "ssh_enabled": presence.ssh_enabled,
        "blocks_incoming": presence.blocks_incoming,
        "observed_at": _moment(presence.observed_at),
    }


def serialize_machine(machine: Any) -> dict[str, Any]:
    label, _tone = machine.state
    return {
        "name": machine.name,
        "node": f"machine:{machine.name}",
        "url": machine.url,
        "state": label,
        "role": machine.role,
        "roles": [{"id": role.id, "label": role.label} for role in machine.roles],
        "declaration": machine.declaration,
        "aliases": list(machine.aliases),
        "addresses": list(machine.addresses),
        "header_addresses": [
            {"label": kind, "address": address, "holder": holder} for kind, address, holder in header_addresses(machine)
        ],
        "hostnames": list(machine.hostnames),
        "runs_hq": machine.runs_hq,
        "containers_visible": machine.containers_visible,
        "hq_hostnames": list(machine.hq_hostnames),
        "reached_by": list(machine.reached_by),
        "opened_by": list(machine.opened_by),
        "unanswered": list(machine.unanswered),
        "resources": list(machine.resources),
        "containers": [
            {
                "name": item.name,
                "stack": item.stack,
                "image": item.image,
                "state": item.state,
                "status": item.status,
                "published": item.published,
                "watcher": item.watcher,
                "observed_at": _moment(item.observed_at),
            }
            for item in machine.containers
        ],
        "presence": _presence(machine.presence),
    }


def list_machines(*, limit: int = 50) -> dict[str, Any]:
    """Every machine anything reported, as the machine list shows them."""

    with projection_scope():
        return _collection([serialize_machine(item) for item in connections.machines_once()[: page_size(limit)]])


def get_machine(name: str) -> dict[str, Any]:
    with projection_scope():
        found = machines.machine(name)
        if found is None:
            raise RecordNotFound("No machine has that name.")
        return serialize_machine(found)


# ----- Domains ----------------------------------------------------------------


def _domains() -> list[dict[str, Any]]:
    estate = estate_reading()
    declared = {
        normalized_hostname(resource.spec.get("zone")): resource.key
        for resource in enabled_resources()
        if resource.kind == ZONE_KIND
    }
    registrations = {item.subject: item for item in estate.registrations}
    members = services_by_zone(estate.domains)
    found = []
    for name in estate.domains:
        registration = registrations.get(name)
        found.append(
            {
                "name": name,
                "node": f"zone:{name}",
                "url": entity_link("zone", name).url,
                "declaration": declared.get(name, ""),
                "services": [
                    {"hostname": member.hostname, "status": member.status} for member in members.get(name, ())
                ],
                "registration": (
                    {
                        "expires": registration.expires.isoformat(),
                        "source": registration.source,
                        "auto_renew": registration.auto_renew,
                    }
                    if registration
                    else None
                ),
            }
        )
    return found


def list_domains(*, limit: int = 50) -> dict[str, Any]:
    """Every domain HQ declares or a sweep has seen, with the services it holds."""

    with projection_scope():
        return _collection(_domains()[: page_size(limit)])


def get_domain(name: str) -> dict[str, Any]:
    """One domain as the list has it, with its records, connection and insights."""

    wanted = normalized_hostname(name)
    with projection_scope():
        found = next((item for item in _domains() if item["name"] == wanted), None)
        context = domain_context(wanted) if found is not None else None
        if found is None or context is None:
            raise RecordNotFound("No domain has that name.")
        zone = context.zone
        return {
            **found,
            "connection_ref": zone.connection_ref,
            "reachable": zone.reachable,
            "observed_at": _moment(zone.observed_at),
            "records": [
                {
                    "name": record.name,
                    "record_type": record.record_type,
                    "value": record.value,
                    "proxied": record.proxied,
                    "ttl": record.ttl,
                    "declaration": record.resource_key,
                    "observed_only": record.observed_only,
                }
                for record in zone.listed
            ],
            "insights": [
                {
                    "label": insight.label,
                    "value": insight.value,
                    "detail": insight.detail,
                    "url": insight.url,
                    "concern": insight.concern,
                    "notice": insight.notice,
                }
                for insight in zone.insights
            ],
        }


# ----- Relationships ----------------------------------------------------------


def get_relationships(node: str, *, principal: Principal) -> dict[str, Any]:
    """One node's edges, grouped by what each says from that node.

    ``node`` is a topology node id: ``machine:<name>``, ``service:<hostname>``,
    ``zone:<domain>``, ``resource:<key>``.
    """

    with projection_scope():
        found = relationships_for(node, principal=principal)
        if not found.known:
            raise RecordNotFound("HQ knows no record by that name.")
        subject = relation_graph(principal=principal).subjects.get(node)
        readings = [
            {
                "kind": kind,
                "label": label,
                "connection": ref,
                "records": list(records),
            }
            for kind, label, ref, records in readout_records(subject)
        ]
    return {
        "node": found.node_id,
        "focus_url": found.focus_url,
        "groups": [
            {
                "phrase": group.phrase,
                "rank": group.rank,
                "items": [
                    {
                        "entity": _link(item.entity),
                        "source": _link(item.source),
                        "observed_at": _moment(item.observed_at),
                        "stale": item.stale,
                    }
                    for item in group.items
                ],
            }
            for group in found.groups
        ],
        "readings": readings,
        "unreadable": list(found.unreadable),
    }


# ----- Paths ------------------------------------------------------------------


def _source(source: Any) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "kind": source.kind,
        "label": source.label,
        "connection": source.connection,
        "observed_at": _moment(source.observed_at),
        "declared": source.declared,
    }


def _certificate(certificate: Any) -> dict[str, Any] | None:
    if certificate is None:
        return None
    return {
        "role": certificate.role,
        "name": certificate.name,
        "issuer": certificate.issuer,
        "expiry": certificate.expiry,
        "source": _source(certificate.source),
        "unread": certificate.unread,
    }


def serialize_hop(hop: Any) -> dict[str, Any]:
    """One hop as every adapter returns it; a hop walked for a request adds
    what the request showed there and the layers decided there."""

    found = {
        "step": hop.step,
        "label": hop.label,
        "name": hop.name,
        "link": _link(hop.link),
        "detail": hop.detail,
        "source": _source(hop.source),
        "certificate": _certificate(hop.certificate),
        "unread": hop.unread,
        "overlays": [{"relation": relation, "link": _link(link)} for relation, link in hop.overlays],
    }
    if hop.check is None:
        return found
    return {
        **found,
        "check": {
            "state": hop.check.state,
            "label": hop.check.label,
            "detail": hop.check.detail,
            "step": hop.check.step,
        },
        "evidence": [
            {"label": item.label, "value": item.value, "detail": item.detail, "role": item.role}
            for item in hop.evidence
        ],
        "layers": [serialize_layer(layer) for layer in hop.layers],
    }


def serialize_layer(layer: Any) -> dict[str, Any]:
    return {
        "id": layer.id,
        "label": layer.label,
        "state": layer.state,
        "detail": layer.detail,
        "evidence": layer.evidence,
        "boundary": layer.boundary,
        "mechanism": layer.mechanism,
        "rules": list(layer.rules),
    }


def serialize_path(path: Any) -> dict[str, Any]:
    """A request path as every adapter returns it."""

    return {
        "hostname": path.hostname,
        "line": path.line,
        "redirects_to": path.redirects_to,
        "machine": path.machine,
        "unread": list(path.gaps),
        "observed": [
            {
                "relation": hop.label,
                "name": hop.name,
                "link": _link(hop.link),
                "detail": hop.detail,
                "source": _source(hop.source),
            }
            for hop in path.observed
        ],
        "routes": [
            {
                "via": route.via,
                "port": route.port,
                "line": route.line,
                "hops": [serialize_hop(hop) for hop in route.hops],
            }
            for route in path.routes
        ],
    }


def get_path(hostname: str) -> dict[str, Any]:
    """The request path to one hostname, hop by hop, as the service page walks it."""

    if not is_hostname(hostname):
        raise RecordNotFound("No path is known for that hostname.")
    wanted = normalized_hostname(hostname)
    with projection_scope():
        return serialize_path(path_to(wanted))


def list_request_path() -> dict[str, Any]:
    """How the calling request reached HQ, as the connection page shows it."""

    request = request_context.current()
    if request is None:
        return {
            **_collection([]),
            "unread": ["not read: request, because this caller made no HTTP request to describe"],
        }

    with projection_scope(seed=serving(request)):
        return serialize_request_path(request_path(request))


# ----- Readings ---------------------------------------------------------------


def _reading(kind: str, row: ProviderInventory | None) -> dict[str, Any]:
    spec = OBSERVATIONS[kind]
    seen = sight(kind, spec.label, "reading", row, permissions=tuple(spec.requires))
    return {
        "kind": kind,
        "label": spec.label,
        "provider": spec.provider,
        "provider_label": CONNECTION_LABELS.get(spec.provider, human_label(spec.provider)),
        "read_by": spec.read_by,
        "facet": spec.facet,
        "relation": spec.relation,
        "requires": list(spec.requires),
        "state": seen.state,
        "records": seen.records,
        "observed_at": _moment(seen.observed_at),
        "error": seen.error,
        "refusal": seen.refusal,
        "remedy": seen.remedy,
    }


def _rows(kinds) -> dict[str, ProviderInventory]:
    return {row.kind: row for row in ProviderInventory.objects.filter(kind__in=list(kinds))}


def list_readings(*, provider: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Every registered reading kind and whether its last read succeeded."""

    kinds = [kind for kind, spec in OBSERVATIONS.items() if provider is None or spec.provider == provider][
        : page_size(limit)
    ]
    rows = _rows(kinds)
    return _collection([_reading(kind, rows.get(kind)) for kind in kinds])


def get_reading(kind: str) -> dict[str, Any]:
    """One reading kind with its stored records, as its schema admits them."""

    spec = OBSERVATIONS.get(kind)
    if spec is None:
        raise RecordNotFound("No reading is of that type.")
    row = _rows((kind,)).get(kind)
    stored = row.records if row is not None and isinstance(row.records, list) and not spec.withheld else []
    return {
        **_reading(kind, row),
        "items": [spec.admitted(record) for record in stored if isinstance(record, dict)],
    }


# ----- Credentials ------------------------------------------------------------


def serialize_provider_sight(found: Any) -> dict[str, Any]:
    return {
        "provider": found.provider,
        "label": found.label,
        "credential_refusal": found.credential_refusal,
        "manages": list(found.manages),
        "tally": [{"count": count, "state": label} for count, label in found.tally],
        "sights": [
            {
                **asdict(item),
                "observed_at": _moment(item.observed_at),
                "state_label": item.state_label,
                "remedy": item.remedy,
            }
            for item in found.sights
        ],
    }


def list_credentials() -> dict[str, Any]:
    """Each connection provider and what its credential can see."""

    return _collection([serialize_provider_sight(found) for found in credential_sight()])


def get_credential(provider: str) -> dict[str, Any]:
    found = next((item for item in credential_sight() if item.provider == provider), None)
    if found is None:
        raise RecordNotFound("No credential is seen for that service.")
    return serialize_provider_sight(found)


# ----- Search -----------------------------------------------------------------


def search(query: str = "", *, limit: int = 20, principal: Principal) -> dict[str, Any]:
    """Machines, services and domains first, then records, as the search page ranks them."""

    bound = page_size(limit)
    if not query.strip():
        return _collection([])
    with projection_scope():
        items = [
            {
                "kind": item.kind,
                "name": item.name,
                "label": item.label,
                "summary": item.summary,
                "url": item.url,
                "group": item.destination_label,
            }
            for item in estate_search(query, principal=principal)
        ]
        for group in global_search(query, principal=principal, limit_per_scope=bound)["groups"]:
            items.extend(
                {
                    "kind": group["scope"],
                    "name": str(hit["id"]),
                    "label": hit["title"],
                    "summary": "".join(text for text, _marked in hit["snippet"]),
                    "url": hit["url"],
                    "group": group["label"],
                }
                for hit in group["items"]
            )
    return _collection(items[:bound])
