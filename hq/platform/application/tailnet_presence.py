"""Where each machine appears on the tailnet.

Its devices, addresses and state, joined to the declared machine they belong
to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


from .tailnet import TAILNET_KIND


@dataclass(frozen=True)
class Presence:
    """Whether a machine is up, said by the network rather than by a service.

    Every other thing HQ knows about a machine is really about something running
    on it, so a box that is switched off and a box whose credential expired look
    the same. This is the one reading that tells them apart, which is why it is
    kept separate from ``reachable`` rather than folded into it.
    """

    online: bool = False
    last_seen: str = ""
    key_expires: str = ""
    addresses: tuple[str, ...] = ()
    # The LAN and public endpoints the device's client reports, "host:port".
    endpoints: tuple[str, ...] = ()
    # What the tailnet calls it, which is rarely what HQ does. Worth showing on
    # the machine's own page: it is the name in the Tailscale console, in
    # MagicDNS, and in an ACL, so an operator moving between HQ and any of
    # those needs the join stated rather than inferred.
    tailnet_name: str = ""
    dns_name: str = ""
    os: str = ""
    offers_exit_node: bool = False
    # Offered and approved are two facts, and only their agreement means the
    # route works. A route is advertised by the machine and must then be
    # approved in the coordination server; until it is, the machine goes on
    # reporting that it offers the route and nothing can use it.
    exit_node_approved: bool = False
    advertised_routes: tuple[str, ...] = ()
    enabled_routes: tuple[str, ...] = ()
    # Facts with no symptom until they matter. A device the tailnet has not
    # authorised reaches nothing; one carrying a lock error cannot be reached
    # by anything under tailnet lock; and a client left behind is how a fleet
    # acquires versions nobody chose.
    authorized: bool = True
    lock_error: str = ""
    update_available: bool = False
    client_version: str = ""
    # Tailscale SSH turns a device into something the policy can hand shells
    # out on. Shields-up means it accepts no inbound connection at all, which
    # from outside looks exactly like being broken. An external device belongs
    # to another tailnet and was shared into this one.
    ssh_enabled: bool = False
    blocks_incoming: bool = False
    external: bool = False
    # The peering itself, as the machine HQ runs on reports it. This reading is
    # taken from HQ's own daemon, so every device in it is a peer of HQ by
    # construction. A key that has completed a
    # handshake, over a path that was negotiated, carrying counted bytes, is
    # the difference between a machine HQ has been told about and one it is
    # actually talking to.
    public_key: str = ""
    direct_endpoint: str = ""
    relay: str = ""
    last_handshake: str = ""
    # Whether this device is the one taking the reading. The peering fields
    # above are observer-relative; the observer's own row is not a peering.
    observer: bool = False
    active: bool = False
    rx_bytes: int = 0
    tx_bytes: int = 0
    tags: tuple[str, ...] = ()
    # When this device last reached HQ, and how: ``arrivals.Arrival``.
    reached_hq: Any = None
    # Who the policy admits, per port. Already swept for the reachability
    # panel, and the same answer a machine's own page should be able to give
    # without anybody having to go and ask it.
    openings: tuple[tuple[int, tuple[str, ...]], ...] = ()
    observed_at: Any = None
    # Who took the reading: the controller, and the connection when the
    # record names one.
    controller_id: str = ""
    connection_ref: str = ""

    @property
    def personal(self) -> bool:
        """A user's own device, whose being offline is not a fault.

        Tailscale's own line between a server and a person's device: a tagged
        node belongs to the tailnet and is infrastructure; an untagged one
        belongs to a user, and a laptop asleep or a phone away is normal.
        """

        return not self.tags

    @property
    def peered(self) -> bool:
        """Whether HQ and this machine have actually completed a handshake.

        Not whether the tailnet lists it. This reading comes from the daemon on
        the machine HQ runs on, so a device appearing at all means HQ has it in
        its network map, but a key in a map is a machine HQ *could* talk to.
        A handshake is one it has.

        Tailscale dates a peer it has never spoken to ``0001-01-01``, so the
        timestamp is parsed rather than tested for emptiness. The observer is
        excluded separately: its ``relay`` is the DERP region it homes to, not
        a path to anywhere.
        """

        from .timestamps import moment

        if self.observer:
            return False
        return bool(self.public_key and moment(self.last_handshake))

    @property
    def handshake(self) -> str:
        """When the two keys last completed a handshake, phrased as HQ phrases
        every other elapsed time."""

        from .moments import elapsed

        return elapsed(self.last_handshake)

    @property
    def peer_path(self) -> str:
        """How the two are reaching each other, in the terms WireGuard uses.

        A direct path means the two daemons found a route through both NATs and
        traffic goes machine to machine. A relayed one means they could not, and
        Tailscale's DERP servers are carrying the encrypted packets: still
        end-to-end encrypted, still slower, and worth knowing which.
        """

        if not self.peered:
            return ""
        if self.direct_endpoint:
            return "direct"
        return "relayed" if self.relay else "negotiating"

    @property
    def unapproved_routes(self) -> tuple[str, ...]:
        """Routes this machine offers that the tailnet has not approved.

        The silent failure this reading exists for. `tailscale up
        --advertise-routes` succeeds, the machine reports the route forever,
        and every other device simply never receives it, so a subnet route or
        an exit node can be declared, believed, and dead, with nothing in the
        estate disagreeing.
        """

        return tuple(
            route
            for route in self.advertised_routes
            if route not in set(self.enabled_routes)
        )

    @property
    def tailnet_address(self) -> str:
        """The device's tailnet IPv4, the address everything on the tailnet uses."""

        from .reach import network_of

        return next(
            (a for a in self.addresses if ":" not in a and network_of(a) == "tailnet"), ""
        )

    @property
    def public_addresses(self) -> tuple[str, ...]:
        """The endpoints' public addresses: not private, tailnet or documentation."""

        from .locate import host_of
        from .reach import is_public

        return tuple(
            dict.fromkeys(
                host for host in (host_of(endpoint) for endpoint in self.endpoints)
                if is_public(host)
            )
        )

    @property
    def key_expiry_days(self) -> int | None:
        """Days until the node key expires, or None when it does not.

        A device with expiry disabled has no expiry, which is not the same as
        an expiry far away: one is a decision and the other is a deadline.
        """

        from .expiry import days_until
        from .timestamps import moment

        when = moment(self.key_expires)
        return days_until(when) if when is not None else None


def tailnet_presence() -> dict[str, Presence]:
    """Presence by machine name, as the tailnet last reported it."""

    found: dict[str, Presence] = {}
    # The same read the policy uses. Three kinds in one table, asked once.
    from hq.domains.control_plane.observations.hq import ARRIVAL_KIND

    from .arrivals import arrivals
    from .tailnet import snapshots

    read = snapshots()
    arrived = arrivals(read[ARRIVAL_KIND])
    for snapshot in read[TAILNET_KIND]:
        for record in snapshot.records:
            name = str(record.get("name", ""))
            if not name:
                continue
            found[name] = Presence(
                online=bool(record.get("online")),
                last_seen=str(record.get("last_seen", "")),
                key_expires=str(record.get("key_expires", "")),
                addresses=tuple(str(a) for a in record.get("addresses") or ()),
                endpoints=tuple(str(e) for e in record.get("endpoints") or ()),
                tailnet_name=name,
                dns_name=str(record.get("dns_name", "")),
                os=str(record.get("os", "")),
                offers_exit_node=bool(record.get("offers_exit_node")),
                exit_node_approved=bool(record.get("exit_node_approved")),
                advertised_routes=tuple(
                    str(r) for r in record.get("advertised_routes") or ()
                ),
                enabled_routes=tuple(
                    str(r) for r in record.get("enabled_routes") or ()
                ),
                authorized=bool(record.get("authorized", True)),
                lock_error=str(record.get("lock_error", "")),
                update_available=bool(record.get("update_available")),
                client_version=str(record.get("client_version", "")),
                ssh_enabled=bool(record.get("ssh_enabled")),
                blocks_incoming=bool(record.get("blocks_incoming")),
                external=bool(record.get("external")),
                public_key=str(record.get("public_key", "")),
                direct_endpoint=str(record.get("direct_endpoint", "")),
                relay=str(record.get("relay", "")),
                last_handshake=str(record.get("last_handshake", "")),
                observer=bool(record.get("self")),
                active=bool(record.get("active")),
                rx_bytes=int(record.get("rx_bytes") or 0),
                tx_bytes=int(record.get("tx_bytes") or 0),
                tags=tuple(str(tag) for tag in record.get("tags") or ()),
                reached_hq=arrived.get(name),
                openings=tuple(
                    (int(entry["port"]), tuple(entry.get("who") or ()))
                    for entry in record.get("reach") or ()
                    if str(entry.get("port", "")).isdigit()
                ),
                observed_at=snapshot.observed_at,
                controller_id=str(snapshot.controller_id or ""),
                connection_ref=str(record.get("connection_ref", "") or ""),
            )
    return found


def observed_addresses() -> dict[str, str]:
    """Every address HQ sees for itself, and what saw it.

    Read from the two sweeps that report where a machine answers: the tailnet
    names every address it hands out, and a container sweep names the host it
    found containers on. Neither is a machine declaration, which is the point,
    these are the addresses nobody needs to type.
    """

    found: dict[str, str] = {}
    for presence in tailnet_presence().values():
        for address in presence.addresses:
            if address:
                found.setdefault(address, "seen on the tailnet")
    from .locate import container_hosts

    for address in container_hosts().values():
        if address:
            found.setdefault(address, "seen when containers were read")
    return found
