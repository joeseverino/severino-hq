"""How this request reached HQ, and what had to be true for it to arrive.

Every other page here describes the estate. This one describes the reader: the
address they came from, the device that address belongs to, the person the
session says they are, and the sequence of independent things that each had to
hold before any of it got this far.

It is assembled rather than asserted. A page claiming "your connection is
secure" is decoration: it says the same words when the gate is switched off,
when the policy has been loosened, and when the request arrived from a coffee
shop. So every line below is read from something that would change if the fact
changed: the settings the middleware actually enforces, the backends actually
installed, the tailnet's own account of who this device is and how its traffic
is being carried, and the access policy as Tailscale evaluated it.

Which means a layer can report that it does *not* hold, and say so plainly.
That is the property that makes the rest worth reading.
"""

from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from hq.platform.core.network import client_ip, is_trusted_proxy, split_host_port

from . import tailnet
from .labels import human_bytes
from .reach import network_of, on_link_networks
from .request_channel import Channel, channel_for_request, displayed_client_ip, forwarded_chain, socket_peer
from .request_identity import Identity, identity_of
from .request_layers import Layer, ServingDeviceResolution, admission_layers
from .ui import MISSING


@dataclass(frozen=True, slots=True)
class Peering:
    """Which network the tailnet link itself is running over.

    Being on the tailnet says the traffic is encrypted and the peer is
    enrolled. It says nothing about where the packets went, and the two are
    routinely confused: a laptop in the same room and a laptop in an airport
    lounge produce an identical page everywhere else in HQ, because both are
    "on the tailnet over WireGuard".

    Tailscale already knows the difference and HQ already stores the answer,
    the endpoint the two nodes negotiated. A private address means they found
    each other on the same network and nothing crossed the internet; a public
    one means this session is riding over it from that address; a relayed path
    means they could not reach each other at all and the traffic is going
    through a machine neither end owns.

    Derived here rather than resolved: no lookup, no egress, and nothing that
    could put DNS in the path of rendering this page.
    """

    id: str
    label: str
    detail: str
    # The public address this session is arriving from, where there is one.
    # Empty for every other state, which is what the surfaces key off: there is
    # nothing to say about the address of a link that never left the house.
    address: str = ""

    @property
    def public(self) -> bool:
        return self.id == "internet"


PEERING_UNKNOWN = Peering(
    "unknown",
    "Not known yet",
    "This device is on the tailnet, and it has no direct or relayed path to "
    "HQ's machine right now.",
)
# Not the same statement. "No peering" is a fact about the device; this is a fact about
# HQ's view of it. Behind a proxy it has not been told to trust, HQ judges the
# proxy and declines to attribute the request to any device at all, so it has
# nothing to read a peering from, which is not evidence that none exists.
PEERING_UNATTRIBUTED = Peering(
    "unattributed",
    "Hidden behind a proxy",
    "This request came through a proxy HQ does not trust, so HQ cannot tell "
    "which device sent it or how that device is connected.",
)
# The sweep is what fills the device inventory, and an instance that has never
# run one (a fresh development database, a deployment whose Tailscale
# connection is not configured) resolves nothing. Saying "not established"
# there would blame the network for an empty table.
PEERING_UNOBSERVED = Peering(
    "unobserved",
    "Tailnet not read",
    "HQ has no reading of the tailnet, so it cannot match this address to a "
    "device. Set up the Tailscale connection, or wait for the next read.",
)


def _peering(presence) -> Peering:
    """Which network a tailnet session rides over, from the caller's presence."""

    if presence is None or not presence.peer_path or presence.peer_path == "negotiating":
        return PEERING_UNKNOWN
    if presence.peer_path == "relayed":
        return Peering(
            "relay",
            f"Relayed via {presence.relay}" if presence.relay else "Relayed",
            "The two machines could not connect directly, so Tailscale "
            "passes the traffic through one of its relays. The relay cannot "
            "read it.",
        )
    host, _ = split_host_port(presence.direct_endpoint)
    where = network_of(host)
    # A global IPv6 address in one of this host's own prefixes is the same
    # network, whatever its type says.
    if where == "public" and any(ip_address(host) in net for net in on_link_networks()):
        where = "network"
    if where in {"network", "loopback"}:
        return Peering(
            "local",
            "Direct, on your own network",
            "The two machines connect directly on the same private network. "
            "Nothing crosses the internet.",
            address=host,
        )
    if where == "public":
        return Peering(
            "internet",
            "Direct, over the internet",
            "The two machines connect directly across the internet, from the "
            "address below. WireGuard encrypts every packet.",
            address=host,
        )
    # A tailnet-range endpoint means the peering is itself being carried by
    # another tailnet hop. Rare, and worth naming rather than guessing at.
    return Peering(
        "indirect",
        "Through another tailnet device",
        "The other end of this link is itself a tailnet address, so another "
        "device on the tailnet is carrying it.",
        address=host,
    )


@dataclass(frozen=True, slots=True)
class Connection:
    """One request, described from the outside in."""

    address: str
    channel: Channel
    device: tailnet.Device | None
    serves: tailnet.Device | None
    observer: tailnet.Device | None
    identity: Identity
    # A device correlated from a forwarded address that HQ will show as
    # evidence but will not use for admission until the forwarding peer is a
    # declared proxy. Keeping it separate from ``device`` prevents display
    # knowledge from quietly becoming authorization knowledge.
    reported_device: tailnet.Device | None = None
    reported_address: str = ""
    layers: tuple[Layer, ...] = ()
    secure_transport: bool = False
    host: str = ""
    # The machine pages behind the two ends, where HQ knows a machine at the
    # address each one answers at. A tailnet name is rarely the name HQ uses,
    # so this is resolved by address rather than by matching the two names.
    machine_url: str = ""
    machine_name: str = ""
    forwarder_name: str = ""
    forwarder_url: str = ""
    forwarded: bool = False
    local_forwarder: bool = False
    serves_url: str = ""
    # What HQ was told about its machines, read once and answering every
    # relationship this page draws: which machine each end of the link is, and
    # which of a node's addresses are ones HQ was actually declared at.
    declared: tuple = ()
    untrusted_forwarding: bool = False
    serves_verified: bool = False
    serving_basis: str = ""
    # The caller device's tailnet presence, as the machine page reads it: the
    # one account of the link's path, handshake and traffic.
    presence: Any = None

    @property
    def holds(self) -> bool:
        return all(layer.holds and layer.conclusive for layer in self.layers)

    @property
    def failing(self) -> tuple[Layer, ...]:
        return tuple(
            layer for layer in self.layers if layer.conclusive and not layer.holds
        )

    @property
    def unverified(self) -> tuple[Layer, ...]:
        return tuple(layer for layer in self.layers if not layer.conclusive)

    @property
    def summary(self) -> str:
        if self.holds:
            return f"All {len(self.layers)} checks passed"
        parts = []
        if self.failing:
            parts.append(f"{len(self.failing)} failed")
        if self.unverified:
            parts.append(f"{len(self.unverified)} not confirmed")
        return " · ".join(parts)

    @property
    def transport(self) -> str:
        """The encryption this request can actually prove."""

        if self.channel.id == "tailnet":
            return "WireGuard + TLS" if self.secure_transport else "WireGuard only"
        return "TLS" if self.secure_transport else "Encryption not confirmed"

    @property
    def transport_path(self) -> str:
        """Which segment each encrypted transport protects."""

        if self.forwarded and self.channel.id == "tailnet" and self.secure_transport:
            if self.local_forwarder:
                return "WireGuard + TLS to HQ's machine"
            return f"TLS to {self.forwarder_name or 'proxy'} · WireGuard to HQ"
        if self.channel.id == "tailnet" and self.secure_transport:
            return "WireGuard + TLS end to end"
        return self.transport

    @property
    def caller_device(self) -> tailnet.Device | None:
        """The device shown as You, without changing the admission device.

        A forwarding peer is a hop, not the caller. Until that peer is trusted,
        its report can identify the diagram's endpoint but never satisfy an
        admission check or corroborate the signed-in person.
        """

        return self.reported_device if self.untrusted_forwarding else self.device

    @property
    def peer_label(self) -> str:
        peer = self.caller_device
        return peer.label if peer else "Device not found"

    @property
    def peer_address(self) -> str:
        return self.reported_address if self.reported_device else self.address

    @property
    def path(self) -> str:
        """direct, relayed or idle, from the caller's presence; unknown without one."""

        if self.presence is None:
            return "unknown"
        return self.presence.peer_path if self.presence.peer_path in {"direct", "relayed"} else "idle"

    @property
    def leg_label(self) -> str:
        """The tailnet leg in a word or two: direct, relayed and where, or not yet."""

        if self.presence is None:
            return "Unknown"
        return {
            "direct": "Direct",
            "relayed": f"Relayed via {self.presence.relay}",
            "idle": "No path yet",
        }[self.path]

    @property
    def path_label(self) -> str:
        if self.forwarded and self.local_forwarder:
            return "Through the proxy on HQ's machine"
        if self.forwarded:
            return f"Through {self.forwarder_name or 'a proxy'}"
        return self.leg_label

    @property
    def link_observed_by_hq(self) -> bool:
        """Whether the daemon measurement is provably from HQ's own node."""

        return bool(
            self.serves_verified
            and self.observer
            and self.serves
            and self.observer.name == self.serves.name
        )

    @property
    def measurement_label(self) -> str:
        if self.link_observed_by_hq:
            return f"HQ's own machine ({self.observer.label})"
        if self.observer:
            return f"Another tailnet machine ({self.observer.label})"
        return "Not measured"

    @property
    def handshake(self) -> str:
        return self.presence.handshake if self.presence is not None else MISSING

    @property
    def carried(self) -> str:
        if self.presence is None:
            return ""
        return f"{human_bytes(self.presence.rx_bytes)} in · {human_bytes(self.presence.tx_bytes)} out"

    @property
    def peer_keys(self) -> tuple[tuple[str, str], ...]:
        """The two node keys behind this connection, yours first.

        The evidence the rest of the link section is describing. Everything
        above it (an endpoint, a handshake age, a byte count) is a
        consequence of these two keys having agreed; naming them is what turns
        "HQ says you are a peer" into something checkable against `tailscale
        status` on either machine.
        """

        found = []
        if self.caller_device and self.caller_device.public_key:
            found.append((self.caller_device.label, self.caller_device.public_key))
        # Path, handshake and byte counters are observer-relative. Pair the
        # caller with the node that actually made that observation, never with
        # a different node merely because it happens to serve HQ.
        if self.observer and self.observer.public_key:
            found.append((self.observer.label, self.observer.public_key))
        return tuple(found)

    @property
    def peering(self) -> Peering:
        """Which network this tailnet session is actually riding over.

        Three ways there is no answer, and they are different sentences. HQ
        may not be able to see the caller at all (an untrusted proxy in front
        of it), may have nothing swept to look the caller up in, or may know
        the device perfectly well and find no path negotiated. Only the last
        of those is a statement about the tailnet.
        """

        if self.caller_device is None:
            if self.untrusted_forwarding or self.forwarded:
                return PEERING_UNATTRIBUTED
            # HQ's own node comes from the same sweep as everyone else's. Not
            # finding itself there means the inventory is empty rather than
            # that this caller is missing from it: read from a field the
            # page already holds, so distinguishing the two costs no query.
            if self.observer is None:
                return PEERING_UNOBSERVED
            return PEERING_UNKNOWN
        return _peering(self.presence)

    @property
    def tailnet_observed_at(self) -> datetime | None:
        """When the device/path evidence was last swept from Tailscale."""

        return self.presence.observed_at if self.presence is not None else None


def connection(request, *, edge=None, firewall=None) -> Connection:
    """Everything HQ can say about the request in front of it."""

    address = client_ip(request)
    peer = socket_peer(request)
    forwarded = bool(forwarded_chain(request))
    forwarding_trusted = forwarded and is_trusted_proxy(peer)
    untrusted_forwarding = forwarded and not forwarding_trusted
    # (see `_serving_device_resolution` for why the observer flag is not the answer)
    # A chain that never named the caller is its own answer, and a more useful
    # one than the class its last proxy happens to fall in. Reporting "local
    # network" here would describe the proxy and read as a fact about the
    # person, which is the one confusion this page exists to prevent.
    channel = channel_for_request(request)
    # One read of the sweep, answering every question asked of it below.
    known = tailnet.devices()
    device = tailnet.device_at(address, known)
    forwarder = tailnet.device_at(peer, known) if forwarded else None
    reported_address = displayed_client_ip(request) if untrusted_forwarding else ""
    reported_device = tailnet.device_at(reported_address, known)
    identity = identity_of(request, None if untrusted_forwarding else device)
    from .infrastructure import declared_machines

    declared = declared_machines()
    from .connections import machines_once
    from .hq_self import hq_service, served_at
    from .tailnet_presence import tailnet_presence

    own = hq_service(request, catalog=machines_once())
    serving = _serving_device_resolution(
        known, declared, served_at(request), machine=own.machine if own else ""
    )
    # A fallback observer is useful provenance, but it is not a placement
    # result. Never use it as HQ's policy target or draw it as HQ's endpoint.
    serves = serving.device if serving.verified else None
    observer = tailnet.observer(known)
    peer_device = reported_device if untrusted_forwarding else device
    presence = tailnet_presence().get(peer_device.name) if peer_device else None
    machine_name = _machine_name(peer_device.addresses if peer_device else (), declared)
    forwarder_name = _machine_name((peer,), declared) if forwarded else ""
    serves_name = _machine_name(serves.addresses if serves else (), declared)
    return Connection(
        address=address,
        channel=channel,
        device=device,
        reported_device=reported_device,
        reported_address=reported_address,
        serves=serves,
        observer=observer,
        machine_url=_machine_url(machine_name),
        machine_name=machine_name,
        forwarder_name=forwarder_name,
        forwarder_url=_machine_url(forwarder_name),
        forwarded=forwarded,
        local_forwarder=forwarded and network_of(peer) == "loopback",
        serves_url=_machine_url(serves_name),
        declared=declared,
        identity=identity,
        untrusted_forwarding=untrusted_forwarding,
        serves_verified=serving.verified,
        serving_basis=serving.basis,
        presence=presence,
        secure_transport=bool(request.is_secure()),
        host=request.get_host(),
        layers=admission_layers(
            request,
            address,
            channel,
            device,
            forwarder,
            serves,
            identity,
            known,
            forwarded=forwarded,
            forwarding_trusted=forwarding_trusted,
            forwarding_peer=peer,
            serving=serving,
            observer=observer,
            edge=edge,
            firewall=firewall,
        ),
    )


def _serving_device_resolution(
    known: dict[str, tailnet.Device],
    declared: tuple[dict[str, object], ...],
    served: tuple[str, ...] = (),
    *,
    machine: str = "",
) -> ServingDeviceResolution:
    """The tailnet node HQ is actually running on.

    The device the sweep marked ``self`` is the controller's host, which is HQ's
    host only when the two share a machine. So the node is found by address
    through ``hq_self``: a device holding one of HQ's own addresses, else the
    device holding an address of the machine ``hq_machine`` places HQ on.

    ``machine`` is the machine catalogue's answer for HQ, the one every page
    shows; a device holding one of its addresses is the next placement. Falls
    back to the observer flag when nothing resolves.
    """

    from .hq_self import hq_machine, own_addresses
    from .locate import index_of

    for address in own_addresses(served):
        device = tailnet.device_at(address, known)
        if device is not None:
            return ServingDeviceResolution(device, True, "found by an address on this machine")
    index = index_of(declared=declared)
    for placed, basis in (
        (hq_machine(index, (), {}, served_at=served, devices=known.values()),
         "found by the machine HQ is recorded on"),
        (machine, "found by the machine HQ's names lead to"),
    ):
        device = _device_on(placed, index, known)
        if device is not None:
            return ServingDeviceResolution(device, True, basis)
    observer = tailnet.observer(known)
    return ServingDeviceResolution(
        observer,
        False,
        "assumed from the machine that read the tailnet" if observer else "not found",
    )


def _device_on(machine: str, index, known: dict[str, tailnet.Device]) -> tailnet.Device | None:
    """The tailnet device holding an address of ``machine``, or None."""

    if not machine:
        return None
    return next(
        (
            device
            for device in known.values()
            if any(index.at(address) == machine for address in device.addresses)
        ),
        None,
    )


def _machine_name(addresses, declared) -> str:
    """The declared machine answering at any of these addresses.

    By address, because the tailnet's name for a machine is rarely the one HQ
    uses (a laptop is whatever its owner typed into it years ago) and the
    address is the one thing every source of a machine agrees on.

    Resolved through the shared index, over the declarations this page has
    already read, so it costs no query, and an address recorded with a port
    matches the same address recorded without one.
    """

    from .locate import index_of

    index = index_of(declared=declared)
    for address in addresses or ():
        name = index.at(address)
        if name:
            return name
    return ""


def _machine_url(name: str) -> str:
    from .entity_links import entity_link

    return entity_link("machine", name).url
