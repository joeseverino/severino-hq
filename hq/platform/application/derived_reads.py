"""What the estate pages derive, as registered read resources.

Each handler asks the function its page asks and projects the answer to JSON,
so nothing here derives a fact. Readings leave only through their schema
(``ObservationSpec.admitted``); no provider payload and no secret is stored,
so none can be returned.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.names import is_hostname, normalized_hostname
from hq.domains.control_plane.observations import OBSERVATIONS

from .json_schemas import as_written
from .labels import human_label
from .own_addresses import tailnet_sightings
from .projection import complete, page_size, projection_scope, window
from .record_errors import RecordNotFound
from .security import Principal
from .standards import CheckResult, Measurement
lazy from . import connections, dashboard, estate, machines, request_context
lazy from .action_items import filter_items
lazy from .command_center import estate_search
lazy from .connection_records import CredentialRecord, TallyPart
lazy from .credential_sight import credential_sight, sight
lazy from .entity_links import entity_link
lazy from .estate import estate_reading
lazy from .hq_self import serving
lazy from .infrastructure import enabled_resources
lazy from .machine_context import header_addresses
lazy from .paths import path_to
lazy from .record_saves import row
lazy from .relationships import readout_records, relationships_for
lazy from .request_path import request_path, serialize_request_path
lazy from .resource_records import (
    CheckPart,
    DnsRecordPart,
    DomainRecord,
    DomainRow,
    HopRow,
    JoinedReadingPart,
    MachineRecord,
    MeasurementPart,
    ObservedPart,
    PathRecord,
    PresencePart,
    ReadingItem,
    ReadingRecord,
    RelationshipsRecord,
    RoutePart,
)
lazy from .search import global_search
lazy from .service_list import services_by_zone
lazy from .topology import relation_graph
lazy from .zones import ZONE_KIND, domain_context


def _moment(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _collection(
    items: Sequence[Any], limit: int | None = None, serialize: Callable[[Any], Any] = lambda item: item
) -> dict[str, Any]:
    """Every match, or the first page of them with how many there were; only
    the page is serialized."""

    if limit is None:
        return complete([serialize(item) for item in items])
    page, paging = window(items, limit)
    return {**paging, "items": [serialize(item) for item in page]}


# ----- Estate -----------------------------------------------------------------


def list_estate(*, principal: Principal) -> dict[str, Any]:
    """The dashboard's estate figures as ``principal`` may read them, each linking to the page that lists it."""

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
                for card in estate.cards(principal=principal)
            ]
        )


def list_action_items(
    *, principal: Principal, query: str = "", status: str | None = None, source: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """The composed action queue as ``principal`` may read it, filtered the way the action items page filters it."""

    with projection_scope():
        queue = dashboard.readable_queue(principal)
        items = filter_items(queue, query=query, status=status or "", source=source or "")
    return _collection(items, limit, dashboard.listed_item)


# ----- Machines ---------------------------------------------------------------


def _presence(presence: Any) -> PresencePart | None:
    if presence is None:
        return None
    return row(presence, PresencePart, lan_address=tailnet_sightings().lan_address(presence))


def serialize_machine(machine: Any) -> MachineRecord:
    return row(
        machine,
        MachineRecord,
        node=f"machine:{machine.name}",
        state=machine.state.label,
        header_addresses=[
            {"label": kind, "address": address, "holder": holder} for kind, address, holder in header_addresses(machine)
        ],
        presence=_presence(machine.presence),
    )


def list_machines(*, limit: int = 50) -> dict[str, Any]:
    """Every machine anything reported, as the machine list shows them."""

    with projection_scope():
        return _collection(connections.machines_once(), limit, serialize_machine)


def get_machine(name: str) -> MachineRecord:
    with projection_scope():
        found = machines.machine(name)
        if found is None:
            raise RecordNotFound("No machine has that name.")
        return serialize_machine(found)


# ----- Domains ----------------------------------------------------------------


def _domains() -> list[DomainRow]:
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
        return _collection(_domains(), limit)


def get_domain(name: str) -> DomainRecord:
    """One domain as the list has it, with its records, connection and insights."""

    wanted = normalized_hostname(name)
    with projection_scope():
        found = next((item for item in _domains() if item["name"] == wanted), None)
        context = domain_context(wanted) if found is not None else None
        if found is None or context is None:
            raise RecordNotFound("No domain has that name.")
        zone = context.zone
        records = [row(record, DnsRecordPart, declaration=record.resource_key) for record in zone.listed]
        return row(zone, DomainRecord, records=records, **found)


# ----- Relationships ----------------------------------------------------------


def get_relationships(node: str, *, principal: Principal) -> RelationshipsRecord:
    """One node's edges, grouped by what each says from that node.

    ``node`` is a topology node id: ``machine:<name>``, ``service:<hostname>``,
    ``zone:<domain>``, ``resource:<key>``.
    """

    with projection_scope():
        found = relationships_for(node, principal=principal)
        if not found.known:
            raise RecordNotFound("HQ knows no record by that name.")
        subject = relation_graph(principal=principal).subjects.get(node)
        readings: list[JoinedReadingPart] = [
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
        "groups": [as_written(group) for group in found.groups],
        "readings": readings,
        "unreadable": list(found.unreadable),
    }


# ----- Paths ------------------------------------------------------------------


def serialize_hop(hop: Any) -> HopRow:
    """One hop as every adapter returns it; a hop walked for a request adds
    what the request showed there and the checks decided there."""

    overlays = [{"relation": relation, "link": as_written(link) if link else None} for relation, link in hop.overlays]
    if hop.check is None:
        return row(hop, HopRow, overlays=overlays)
    layers = [serialize_check(result) for result in hop.checks]
    return row(hop, HopRow, overlays=overlays, check=hop.check, evidence=hop.evidence, layers=layers)


def serialize_check(result: CheckResult) -> CheckPart:
    """One check's result, of whichever standard, as the API answers it."""

    check, reading = result.check, result.reading
    return {
        "id": check.id,
        "label": check.label,
        "state": result.state,
        "limited": reading.limited,
        "serious": check.serious,
        "detail": result.reason or reading.detail,
        "evidence": reading.evidence,
        "why": check.why_said,
        "fix": check.fix,
        "read_with": reading.fix,
        "read_with_url": reading.fix_url,
        "boundary": check.boundary,
        "mechanism": check.mechanism,
        "rules": list(reading.rules),
    }


def serialize_measurement(found: Measurement) -> MeasurementPart:
    """One subject against a standard, as the API answers it."""

    return row(found, MeasurementPart, checks=[serialize_check(result) for result in found.results])


def serialize_path(path: Any) -> PathRecord:
    """A request path as every adapter returns it."""

    return row(
        path,
        PathRecord,
        unread=path.gaps,
        observed=[row(hop, ObservedPart, relation=hop.label) for hop in path.observed],
        routes=[row(route, RoutePart, hops=[serialize_hop(hop) for hop in route.hops]) for route in path.routes],
    )


def get_path(hostname: str) -> PathRecord:
    """The request path to one hostname, hop by hop, as the service page walks it."""

    if not is_hostname(hostname):
        raise RecordNotFound("No path is known for that hostname.")
    wanted = normalized_hostname(hostname)
    with projection_scope():
        return serialize_path(path_to(wanted))


def list_request_path(*, principal: Principal | None = None) -> dict[str, Any]:
    """How the calling request reached HQ, as the connection page shows it,
    an agent's judged by the approval of the device it called from."""

    request = request_context.current()
    if request is None:
        return {
            **_collection([]),
            "unread": ["not read: request, because this caller made no HTTP request to describe"],
        }

    with projection_scope(seed=serving(request)):
        return serialize_request_path(request_path(request, principal))


# ----- Readings ---------------------------------------------------------------


def _reading(kind: str, row: ProviderInventory | None) -> ReadingItem:
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

    kinds = [kind for kind, spec in OBSERVATIONS.items() if provider is None or spec.provider == provider]
    rows = _rows(kinds)
    return _collection(kinds, limit, lambda kind: _reading(kind, rows.get(kind)))


def get_reading(kind: str) -> ReadingRecord:
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


def tally(found: Any) -> list[TallyPart]:
    """How many of what one service's credential reads are in each state."""

    return [{"count": count, "state": label} for count, label in found.tally]


def serialize_provider_sight(found: Any) -> CredentialRecord:
    return row(found, CredentialRecord, tally=tally(found))


def list_credentials() -> dict[str, Any]:
    """Each connection provider and what its credential can see."""

    return _collection([serialize_provider_sight(found) for found in credential_sight()])


def get_credential(provider: str) -> CredentialRecord:
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
    # Each scope was cut at the bound before merging, so the merged length is
    # not how many matched: the total stays unknown and a full page reads as cut.
    page = items[:bound]
    return {"items": page, "count": len(page), "total": None}
