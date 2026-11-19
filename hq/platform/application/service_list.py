"""The services a page lists, and the services read resource.

Every declared service, HQ's own name marked, and each name a service-naming
reading holds in a known domain that nothing declares, marked observed. The
list page, the topology, the domain cards and the API read this one list.
"""

from dataclasses import dataclass, replace
from typing import Any

from hq.domains.control_plane.names import is_hostname, normalized_hostname

from .adoption import ADOPT, AdoptCommand, adopt
from .contracts import Effect
from .integration_specs import CapabilitySpec, ListRead, ResourceSpec, typed_keys
from .path_records import PathRecord
from .projection import complete, projection_scope, read_once
from .record_errors import RecordNotFound
from .resources import EmptyQuery
from .security import Capability
from .service_facets import zone_holding
from .service_records import FacetPart, ServiceItem, ServiceRecord, ServiceRow
from .services import (
    CONTAINER_KIND,
    HQ_MARK,
    LISTED,
    OBSERVED_MARK,
    PROSPECT,
    Service,
    ordered_services,
    prospects,
    service_catalog,
)
from .tone import Tone
lazy from . import connections, zones
lazy from .derived_reads import get_path, serialize_path
lazy from .facts import readings
lazy from .hq_self import hq_service
lazy from .record_saves import row


def listed_services(favorites: tuple[str, ...] = ()) -> tuple[Service, ...]:
    """Every service a page lists: the declared ones, HQ's own, and observed names.

    HQ's own name is marked on its row, declared or not. A name a reading
    serves (``names_services``) in a domain HQ knows, that nothing declares,
    is a service marked observed, so a link to it lands on its page.
    """

    return read_once(f"services.listed:{'|'.join(favorites)}", lambda: _listed(favorites))


def _listed(favorites: tuple[str, ...]) -> tuple[Service, ...]:
    catalog = service_catalog()
    own = hq_service(catalog=connections.machines_once())
    own_names = frozenset(own.hostnames) if own is not None else frozenset()
    declared = {service.hostname for service in catalog}
    folded = {alias for service in catalog for alias in service.aliases}
    wanted: dict[str, str] = {own.hostname: HQ_MARK} if own is not None else {}
    for name in observed_names():
        wanted.setdefault(name, OBSERVED_MARK)
    wanted = {name: mark for name, mark in wanted.items() if name not in declared and name not in folded}
    marked = tuple(replace(service, mark=HQ_MARK) if service.hostname in own_names else service for service in catalog)
    unlisted = tuple(replace(service, mark=wanted[service.hostname]) for service in prospects(tuple(wanted)))
    return ordered_services(tuple(sorted(marked + unlisted, key=lambda service: service.hostname)), favorites)


def listed_service(hostname: str) -> Service:
    """The listed service for a name, with its mark; a prospect for any other name."""

    wanted = normalized_hostname(hostname)
    return (
        next((service for service in listed_services() if service.hostname == wanted), None) or prospects((wanted,))[0]
    )


def observed_names() -> tuple[str, ...]:
    """Hostnames a service-naming reading holds, under a domain HQ knows."""

    domains = zones.zone_names()
    found = {
        name for name in readings().hostnames(names_services=True) if is_hostname(name) and zone_holding(name, domains)
    }
    return tuple(sorted(found))


def serialize_service(service: Service) -> ServiceItem:
    return row(
        service,
        ServiceItem,
        facets=[
            row(facet, FacetPart, resources=[claim.resource_key for claim in facet.claims]) for facet in service.facets
        ],
        project=service.project["name"] if service.project else None,
    )


def list_services() -> dict[str, Any]:
    """Every listed hostname, HQ's own and observed ones marked, and its wiring."""

    with projection_scope():
        items = [serialize_service(service) for service in listed_services()]
    return complete(items)


def get_service(hostname: str) -> ServiceRecord:
    """One hostname with the resources behind each facet and its request path:
    a listed one, or any other hostname, which nothing serves yet."""

    wanted = normalized_hostname(hostname)
    with projection_scope():
        found = next((service for service in listed_services() if service.hostname == wanted), None)
        listing = LISTED
        if found is None:
            if not is_hostname(wanted):
                raise RecordNotFound("That is not a hostname.")
            (found,), listing = prospects((wanted,)), PROSPECT
        return {**serialize_service(found), "url": found.url, "listing": listing, "path": serialize_path(found.path)}


@dataclass(frozen=True, slots=True)
class ZoneMember:
    """A service a domain holds: one from the catalog, or HQ's own."""

    hostname: str
    url: str
    status: str = Tone.NEUTRAL
    status_label: str = ""
    service: Service | None = None


def services_by_zone(zones) -> dict[str, tuple[ZoneMember, ...]]:
    """Each domain's services, HQ's own included, under the most specific domain."""

    members = [
        ZoneMember(service.hostname, service.url, service.tone, service.status_label, service)
        for service in listed_services()
    ]
    found: dict[str, list[ZoneMember]] = {}
    for member in members:
        zone = zone_holding(member.hostname, zones)
        if zone:
            found.setdefault(zone, []).append(member)
    return {zone: tuple(items) for zone, items in found.items()}


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            ADOPT,
            "Manage a record HQ found live, starting from exactly what is live.",
            Effect.REMOTE_WRITE,
            Capability.MANAGE_INFRASTRUCTURE,
            AdoptCommand,
            adopt,
            subject_resource="infrastructure",
            execution_notes=(
                "Read the record again as HQ last found it live, never from the request.",
                "Write HQ's copy equal to it, so applying it changes nothing.",
            ),
            label="Manage a record in HQ",
            verb="Manage in HQ",
            variants=((CONTAINER_KIND, "Watch container"),),
            wants="manage a record HQ found live",
            reached_from="control_plane:services",
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "services",
            "Services",
            "Every service name and the state of its DNS, proxy and certificate.",
            Capability.READ,
            listing=ListRead(list_services, EmptyQuery, ServiceItem, fields=typed_keys(ServiceRow)),
            detail_handler=get_service,
            identifier="hostname",
            web_route="control_plane:services",
            record_type=ServiceRecord,
        ),
        ResourceSpec(
            "service.paths",
            "Service paths",
            "The path a request for a service's hostname takes, hop by hop, with each hop's reading and certificate.",
            Capability.READ,
            detail_handler=get_path,
            identifier="hostname",
            web_route="control_plane:services",
            record_type=PathRecord,
        ),
    )
