"""Readings HQ takes itself from the keyless public registries.

RDAP needs no credential, so HQ reads it rather than a controller. ``refresh``
runs in a request the browser makes after a page, never during one: it reads
only subjects with no record or one older than ``REFRESH_AFTER``, at most
``LOOKUPS_PER_REFRESH`` of them, and stores the result through the same ingest
as a sweep. Pages read the stored reading through ``application.facts``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from control_plane.dns_lookup import (
    LookupNotFound,
    LookupUnavailable,
    domain_registry,
    registry,
)
from control_plane.observations.public_registry import ADDRESS_KIND, DOMAIN_KIND

from . import readings
from .facts import record_read_at
from .locate import host_of
from .lookup import HOSTNAME, allocation_of, entity_name
from .reach import is_public, public_label
from .security import Capability, Principal

REFRESH_AFTER = timedelta(days=1)
# How long a stored reading stands before a page's request looks at it again.
CHECK_AFTER = timedelta(hours=1)
LOOKUPS_PER_REFRESH = 16
# The stored reading that says when a page last asked for a refresh.
CHECKED = "public-registry:checked"


def _configured() -> bool:
    endpoint = str(getattr(settings, "SEVERINO_RDAP_ENDPOINT", "") or "").strip()
    parsed = urlsplit(endpoint)
    return parsed.scheme == "https" and bool(parsed.hostname)


def public_address(value: str) -> str:
    """The address, normalised, when it is routable on the public internet."""

    try:
        parsed = ip_address(host_of(value))
    except ValueError:
        return ""
    return str(parsed) if is_public(str(parsed)) else ""


def wanted_addresses() -> tuple[str, ...]:
    """Public addresses a service is served from, a machine is declared at, or a
    machine's tailnet client reports."""

    from .infrastructure import declared_machines
    from .machines import tailnet_presence
    from .services import service_catalog

    found = {
        public_address(service.origin.address)
        for service in service_catalog()
        if service.origin is not None
    }
    for machine in declared_machines():
        found.update(public_address(str(item)) for item in machine.get("addresses") or ())
    # Already filtered to public addresses by the reach ranges.
    for presence in tailnet_presence().values():
        found.update(presence.public_addresses)
    return tuple(sorted(address for address in found if address))


def holders(addresses) -> dict[str, str]:
    """Who holds each public address, from the stored registry reading."""

    from .facts import Subject, readings

    index = readings()
    found = {}
    for address in addresses:
        holder = next(
            (
                item.title
                for item in index.about(Subject.of(addresses=(address,)), kinds=(ADDRESS_KIND,))
                if item.title
            ),
            "",
        )
        found[address] = holder
    return found


def public_endpoints(addresses) -> tuple[tuple[str, str], ...]:
    """``(address, holder)`` with each IPv6 address folded into its /64, once.

    A client rotates IPv6 privacy addresses inside one /64; the prefix is the
    stable fact. The holder is the first one read for any address it folds.
    """

    held = holders(addresses)
    shown: dict[str, str] = {}
    for address in addresses:
        label = public_label(address)
        if label and not shown.get(label):
            shown[label] = held.get(address, "")
    return tuple(shown.items())


def wanted_domains() -> tuple[str, ...]:
    from .zones import zone_names

    return tuple(name for name in zone_names() if HOSTNAME.match(name))


def read_address(address: str, allocations: Callable[..., dict] = registry) -> dict[str, Any]:
    held = allocations(address)
    allocation = allocation_of(held)
    return {
        "address": address,
        "organisation": allocation["organisation"],
        "network": allocation["name"],
        "handle": allocation["handle"],
        "country": allocation["country"],
        "range": allocation["range"],
    }


def read_domain(domain: str, registrations: Callable[..., dict] = domain_registry) -> dict[str, Any]:
    held = registrations(domain)
    events = {
        str(event.get("eventAction", "")): str(event.get("eventDate", ""))
        for event in held.get("events") or ()
        if isinstance(event, dict)
    }
    return {
        "domain": domain,
        "registrar": entity_name(held, "registrar"),
        "registered_at": events.get("registration", ""),
        "expires_at": events.get("expiration", ""),
        "status": [str(item) for item in held.get("status") or () if item],
    }


def _report(
    kind: str,
    key: str,
    subjects: Iterable[str],
    read: Callable[[str], dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """One kind's payload for the ingest, or None when nothing is due."""

    from control_plane.models import ProviderInventory

    stored = ProviderInventory.objects.filter(kind=kind).first()
    kept = {
        str(record.get(key, "")): record
        for record in (stored.records if stored is not None else ())
    }
    wanted = tuple(dict.fromkeys(subjects))
    due = [
        subject
        for subject in wanted
        if subject not in kept
        or (record_read_at(kept[subject]) or datetime.min.replace(tzinfo=now.tzinfo))
        < now - REFRESH_AFTER
    ]
    dropped = set(kept) - set(wanted)
    if not due and not dropped and stored is not None and stored.reachable:
        return None
    answered = 0
    failures: list[str] = []
    for subject in due[:LOOKUPS_PER_REFRESH]:
        try:
            record = read(subject)
        except LookupNotFound as exc:
            record = {key: subject, "unread": str(exc)}
            answered += 1
        except LookupUnavailable as exc:
            failures.append(str(exc))
            record = {key: subject, "unread": str(exc)}
        else:
            answered += 1
        kept[subject] = {**record, "read_at": now.isoformat()}
    if failures and not answered:
        return {"ok": False, "records": [], "error": failures[0]}
    return {"ok": True, "records": [kept[subject] for subject in wanted if subject in kept]}


def refresh(
    *,
    principal: Principal,
    addresses: Iterable[str] | None = None,
    domains: Iterable[str] | None = None,
    allocations: Callable[..., dict] = registry,
    registrations: Callable[..., dict] = domain_registry,
    force: bool = False,
) -> dict[str, Any]:
    """Read what is due from the public registries and store it."""

    from .inventory import record_inventory

    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    now = timezone.now()
    kinds = (ADDRESS_KIND, DOMAIN_KIND)
    checked = readings.stored(CHECKED)
    if not force and checked is not None and now - checked.observed_at < CHECK_AFTER:
        return {"ok": True, "recorded": []}
    readings.record(CHECKED, {}, observed_at=now)
    if not _configured():
        return record_inventory(
            {kind: {"ok": True, "records": [], "connected": False} for kind in kinds},
            principal=principal,
        )
    payload = {
        kind: report
        for kind, report in (
            (
                ADDRESS_KIND,
                _report(
                    ADDRESS_KIND,
                    "address",
                    wanted_addresses() if addresses is None else addresses,
                    lambda address: read_address(address, allocations),
                    now,
                ),
            ),
            (
                DOMAIN_KIND,
                _report(
                    DOMAIN_KIND,
                    "domain",
                    wanted_domains() if domains is None else domains,
                    lambda domain: read_domain(domain, registrations),
                    now,
                ),
            ),
        )
        if report is not None
    }
    if not payload:
        return {"ok": True, "recorded": []}
    return record_inventory(payload, principal=principal)


# The HQ-side reader for each reading HQ takes itself; see ``read_by``.
READERS: dict[str, Callable[..., dict[str, Any]]] = {
    ADDRESS_KIND: read_address,
    DOMAIN_KIND: read_domain,
}
