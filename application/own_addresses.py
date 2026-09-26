"""Which of a device's reported endpoints are its own.

A tailnet client reports every address it could be reached at: its LAN
address, container bridges, and the public address it is seen from. Which of
those belong to the device is read off the whole tailnet, not configured:

- A LAN address sits inside a subnet route some device advertises, or shares
  a network with another device's different address. A container bridge
  (the same gateway address on every Docker host) shares neither.
- A public address is the device's own when no other device is seen from it
  and the device is on no LAN. One several devices report is the NAT gateway
  in front of them; one a device on a LAN reports alone is a network it
  roamed through (a phone's carrier).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network

from .locate import host_of
from .reach import network_of, public_label

# A private network wide enough to hold one LAN, narrow enough not to join two.
_LAN_PREFIX = 24


def _private_hosts(presence) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            host
            for host in (host_of(endpoint) for endpoint in presence.endpoints)
            if ":" not in host and network_of(host) == "network"
        )
    )


@dataclass(frozen=True)
class Sightings:
    """What every device on the tailnet reports, counted once."""

    routes: tuple = ()
    # Private /24 -> the distinct addresses devices report inside it.
    networks: dict = field(default_factory=dict)
    # Public address or /64 -> how many devices report it.
    public: Counter = field(default_factory=Counter)

    def lan_address(self, presence) -> str:
        hosts = _private_hosts(presence)
        routed = next(
            (host for host in hosts if any(ip_address(host) in route for route in self.routes)),
            "",
        )
        if routed:
            return routed
        return next(
            (host for host in hosts if len(self.networks.get(_network(host), ())) > 1), ""
        )

    def public_addresses(self, presence) -> tuple[str, ...]:
        if self.lan_address(presence):
            return ()
        return tuple(
            host for host in presence.public_addresses if self.public[public_label(host)] <= 1
        )


def _network(host: str) -> str:
    return str(ip_network(f"{host}/{_LAN_PREFIX}", strict=False))


def sightings(presences) -> Sightings:
    routes = []
    networks: dict[str, set[str]] = {}
    public: Counter = Counter()
    for presence in presences:
        for route in presence.advertised_routes:
            try:
                network = ip_network(route, strict=False)
            except ValueError:
                continue
            if network.prefixlen and network_of(str(network.network_address)) == "network":
                routes.append(network)
        for host in _private_hosts(presence):
            networks.setdefault(_network(host), set()).add(host)
        public.update({public_label(host) for host in presence.public_addresses})
    return Sightings(tuple(routes), networks, public)


def tailnet_sightings() -> Sightings:
    """The sightings for the machine catalogue, read once per projection."""

    from .connections import machines_once
    from .projection import read_once

    return read_once(
        "machines.sightings",
        lambda: sightings(
            machine.presence for machine in machines_once() if machine.presence is not None
        ),
    )
