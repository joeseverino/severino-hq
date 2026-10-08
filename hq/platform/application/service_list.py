"""The services a page lists, and the services read resource.

Every declared service, HQ's own name marked, and each name a service-naming
reading holds in a known domain that nothing declares, marked observed. The
list page, the topology, the domain cards and the API read this one list.
"""

from dataclasses import dataclass, replace
from typing import Any

from hq.domains.control_plane.names import normalized_hostname

from .infrastructure import NotFoundError
from .projection import read_once
from .services import (
    HQ_MARK,
    OBSERVED_MARK,
    Service,
    ordered_services,
    prospects,
    service_catalog,
)
from .service_facets import zone_holding


def listed_services(favorites: tuple[str, ...] = ()) -> tuple[Service, ...]:
    """Every service a page lists: the declared ones, HQ's own, and observed names.

    HQ's own name is marked on its row, declared or not. A name a reading
    serves (``names_services``) in a domain HQ knows, that nothing declares,
    is a service marked observed, so a link to it lands on its page.
    """

    return read_once(
        f"services.listed:{'|'.join(favorites)}", lambda: _listed(favorites)
    )


def _listed(favorites: tuple[str, ...]) -> tuple[Service, ...]:
    from .connections import machines_once
    from .hq_self import hq_service

    catalog = service_catalog()
    own = hq_service(catalog=machines_once())
    own_names = frozenset(own.hostnames) if own is not None else frozenset()
    declared = {service.hostname for service in catalog}
    folded = {alias for service in catalog for alias in service.aliases}
    wanted: dict[str, str] = {own.hostname: HQ_MARK} if own is not None else {}
    for name in observed_names():
        wanted.setdefault(name, OBSERVED_MARK)
    wanted = {
        name: mark
        for name, mark in wanted.items()
        if name not in declared and name not in folded
    }
    marked = tuple(
        replace(service, mark=HQ_MARK) if service.hostname in own_names else service
        for service in catalog
    )
    unlisted = tuple(
        replace(service, mark=wanted[service.hostname])
        for service in prospects(tuple(wanted))
    )
    return ordered_services(
        tuple(sorted(marked + unlisted, key=lambda service: service.hostname)), favorites
    )


def listed_service(hostname: str) -> Service:
    """The listed service for a name, with its mark; a prospect for any other name."""

    wanted = normalized_hostname(hostname)
    return next(
        (service for service in listed_services() if service.hostname == wanted), None
    ) or prospects((wanted,))[0]


def observed_names() -> tuple[str, ...]:
    """Hostnames a service-naming reading holds, under a domain HQ knows."""

    from hq.domains.control_plane.names import is_hostname

    from .facts import readings
    from .zones import zone_names

    zones = zone_names()
    found = {
        name
        for name in readings().hostnames(names_services=True)
        if is_hostname(name) and zone_holding(name, zones)
    }
    return tuple(sorted(found))


def serialize_service(service: Service) -> dict[str, Any]:
    return {
        "hostname": service.hostname,
        "mark": service.mark,
        "status": service.status,
        "status_label": service.status_label,
        "facets": [
            {
                "id": facet.id,
                "label": facet.label,
                "present": facet.present,
                "state": facet.state,
                "resources": [claim.resource_key for claim in facet.claims],
            }
            for facet in service.facets
        ],
        "origin": (
            {
                "address": service.origin.address,
                "host": service.origin.host,
                "container": service.origin.container,
            }
            if service.origin
            else None
        ),
        "project": service.project["name"] if service.project else None,
        "faults": list(service.faults),
    }


def list_services() -> dict[str, Any]:
    """Every listed hostname, HQ's own and observed ones marked, and its wiring."""

    from .projection import projection_scope

    with projection_scope():
        items = [serialize_service(service) for service in listed_services()]
    return {"items": items, "count": len(items)}


def get_service(hostname: str) -> dict[str, Any]:
    """One listed hostname, with the resources behind each facet and its request path."""

    from .derived_reads import serialize_path
    from .projection import projection_scope

    wanted = normalized_hostname(hostname)
    with projection_scope():
        found = next(
            (service for service in listed_services() if service.hostname == wanted), None
        )
        if found is None:
            raise NotFoundError(f"No service is listed for {hostname!r}.")
        return {"service": serialize_service(found), "path": serialize_path(found.path)}


@dataclass(frozen=True)
class ZoneMember:
    """A service a domain holds: one from the catalogue, or HQ's own."""

    hostname: str
    url: str
    status: str = "neutral"
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
