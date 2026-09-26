"""What the estate pages derive, as registered read resources.

Each handler asks the function its page asks and projects the answer to JSON.
Nothing here derives a fact: the estate reading, the machine and service
catalogues, the relation graph, the join engine, credential sight and the
action queue do. Readings leave only through their schema
(``ObservationSpec.admitted``); no provider payload and no secret is stored,
so none can be returned.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from control_plane.names import normalized_hostname
from control_plane.models import ProviderInventory
from control_plane.observations import OBSERVATIONS
from control_plane.providers import CONNECTION_LABELS

from .labels import human_label
from .own_addresses import tailnet_sightings
from .projection import page_size, projection_scope
from .security import Principal


class NotFoundError(ValueError):
    """No derived record answers to the identifier."""


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

    from .estate import cards

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
                for card in cards()
            ]
        )


def list_action_items(
    *, query: str = "", status: str = "", source: str = "", limit: int = 50
) -> dict[str, Any]:
    """The composed action queue, filtered the way the action items page filters it."""

    from .action_items import filter_items
    from .dashboard import work_queue

    with projection_scope():
        items = filter_items(work_queue(), query=query, status=status, source=source)
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
    from .machine_context import header_addresses

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
            {"label": kind, "address": address, "holder": holder}
            for kind, address, holder in header_addresses(machine)
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

    from .connections import machines_once

    with projection_scope():
        return _collection(
            [serialize_machine(item) for item in machines_once()[: page_size(limit)]]
        )


def get_machine(name: str) -> dict[str, Any]:
    from .machines import machine

    with projection_scope():
        found = machine(name)
        if found is None:
            raise NotFoundError(name)
        return serialize_machine(found)


# ----- Domains ----------------------------------------------------------------


def _domains() -> list[dict[str, Any]]:
    from .estate import estate_reading
    from .infrastructure import enabled_resources
    from .services import services_by_zone
    from .zones import ZONE_KIND
    from .entity_links import entity_link

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
                    {"hostname": member.hostname, "status": member.status}
                    for member in members.get(name, ())
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

    from .zones import domain_context

    wanted = normalized_hostname(name)
    with projection_scope():
        found = next((item for item in _domains() if item["name"] == wanted), None)
        context = domain_context(wanted) if found is not None else None
        if found is None or context is None:
            raise NotFoundError(name)
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

    from .relationships import readout_records, relationships_for
    from .topology import relation_graph

    with projection_scope():
        found = relationships_for(node, principal=principal)
        if not found.known:
            raise NotFoundError(node)
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


# ----- Readings ---------------------------------------------------------------


def _reading(kind: str, row: ProviderInventory | None) -> dict[str, Any]:
    from .credential_sight import sight

    spec = OBSERVATIONS[kind]
    seen = sight(kind, spec.label, "reading", row, requires=", ".join(spec.requires))
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

    kinds = [
        kind
        for kind, spec in OBSERVATIONS.items()
        if provider is None or spec.provider == provider
    ][: page_size(limit)]
    rows = _rows(kinds)
    return _collection([_reading(kind, rows.get(kind)) for kind in kinds])


def get_reading(kind: str) -> dict[str, Any]:
    """One reading kind with its stored records, as its schema admits them."""

    spec = OBSERVATIONS.get(kind)
    if spec is None:
        raise NotFoundError(kind)
    row = _rows((kind,)).get(kind)
    stored = row.records if row is not None and isinstance(row.records, list) else []
    return {
        **_reading(kind, row),
        "items": [spec.admitted(record) for record in stored if isinstance(record, dict)],
    }


# ----- Credentials ------------------------------------------------------------


def _provider_sight(found: Any) -> dict[str, Any]:
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

    from .credential_sight import credential_sight

    return _collection([_provider_sight(found) for found in credential_sight()])


def get_credential(provider: str) -> dict[str, Any]:
    from .credential_sight import credential_sight

    found = next((item for item in credential_sight() if item.provider == provider), None)
    if found is None:
        raise NotFoundError(provider)
    return _provider_sight(found)


# ----- Search -----------------------------------------------------------------


def search(query: str = "", *, limit: int = 20, principal: Principal) -> dict[str, Any]:
    """Machines, services and domains first, then records, as the search page ranks them."""

    from .command_center import estate_search
    from .search import global_search

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
