"""Readings a controller takes of the machine it runs on and the edges it can see."""

from __future__ import annotations

from .contract import ObservationRecord, ObservationSpec

class HostFirewallRecord(ObservationRecord):
    record: str
    interface: str = ""
    accept_requires_interface: bool = False
    foreign_interface_dropped: bool = False
    read_at: str = ""


class HostPerimeterRecord(ObservationRecord):
    record: str
    connection_ref: str
    firewall_unit: str = "unknown"
    public_addresses: tuple[str, ...] = ()
    ports_checked: tuple[int, ...] = ()
    answered_publicly: tuple[int, ...] = ()
    read_at: str = ""


FIREWALL_KIND = "host.firewall"
PERIMETER_KIND = "host.perimeter"

OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        FIREWALL_KIND,
        "host",
        "Host firewall",
        HostFirewallRecord,
        title=lambda record: str(record.get("interface", "")),
        relation="Firewall on interface",
    ),
    ObservationSpec(
        PERIMETER_KIND,
        "ssh",
        "Public perimeter",
        HostPerimeterRecord,
        addresses=lambda record: tuple(record.get("public_addresses") or ()),
        title=lambda record: str(record.get("connection_ref", "")),
        relation="Perimeter checked through",
    ),
)
