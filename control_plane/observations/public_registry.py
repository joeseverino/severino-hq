"""Readings HQ takes itself from the keyless public registries.

RDAP needs no credential, so HQ reads it rather than a controller, after a page
rather than during one, and stores it through the same ingest as a sweep.
"""

from __future__ import annotations

from ..names import normalized_hostname
from .contract import ObservationRecord, ObservationSpec

ADDRESS_KIND = "registry.address"
DOMAIN_KIND = "registry.domain"


class AddressHolderRecord(ObservationRecord):
    address: str
    # The organisation the allocation names, and the network's own name.
    organisation: str = ""
    network: str = ""
    handle: str = ""
    country: str = ""
    range: str = ""
    read_at: str = ""
    unread: str = ""


class DomainRegistrationRecord(ObservationRecord):
    domain: str
    registrar: str = ""
    registered_at: str = ""
    expires_at: str = ""
    status: tuple[str, ...] = ()
    read_at: str = ""
    unread: str = ""


def _address(record) -> tuple[str, ...]:
    address = str(record.get("address", "") or "").strip()
    return (address,) if address else ()


def _domain(record) -> tuple[str, ...]:
    domain = normalized_hostname(record.get("domain"))
    return (domain,) if domain else ()


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        ADDRESS_KIND,
        "rdap",
        "Address registration",
        AddressHolderRecord,
        addresses=_address,
        title=lambda record: str(
            record.get("organisation") or record.get("network") or ""
        ),
        relation="Address held by",
        facet="network",
        read_by="hq",
    ),
    ObservationSpec(
        DOMAIN_KIND,
        "rdap",
        "Domain registration",
        DomainRegistrationRecord,
        hostnames=_domain,
        title=lambda record: str(record.get("registrar") or ""),
        relation="Registered through",
        facet="registration",
        expires=lambda record: str(record.get("expires_at", "")),
        read_by="hq",
    ),
)
