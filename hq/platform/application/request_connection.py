"""How this request reached HQ, and what had to be true for it to arrive.

It describes the reader: their address, device and person, and each layer that
had to hold for the request to get this far. Every line is read from something
that would change if the fact changed (the settings the middleware enforces,
the installed backends, the tailnet, the policy as Tailscale evaluated it), so
a layer can say it does not hold, where "secure" would stay with the gate off.
"""

from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from hq.platform.core.network import client_ip, is_trusted_proxy, socket_peer, split_host_port

from . import tailnet
from .freshness import PAGE_VISIT, freshness
from .labels import dotted, human_bytes
from .reach import network_of, on_link_networks
from .request_channel import Channel, channel_for_request, displayed_client_ip, encryption, forwarded_chain
from .request_identity import Identity, identity_of
from .request_layers import Layer, ServingDeviceResolution, admission_layers
from .ui import MISSING, counted
lazy from . import connections
lazy from .entity_links import entity_link
lazy from .hq_self import hq_machine, hq_service, own_addresses, served_at
lazy from .infrastructure import declared_machines
lazy from .locate import index_of
lazy from .moments import ago
lazy from .tailnet_presence import tailnet_presence
lazy from .timestamps import moment


@dataclass(frozen=True, slots=True)
class Peering:
    """How the tailnet link is carried, named as Tailscale names a connection
    type: direct, relayed through DERP, or through a peer relay.

    A direct link also says whether it stays on the reader's network or
    crosses the internet, which nothing else on the page shows. Derived from
    the endpoint the two nodes negotiated, which HQ already stores, so no
    lookup stands in the path of rendering the page (``_peering``).
    """

    id: str
    label: str
    # The public address a direct link crosses the internet from; empty for
    # every other link, which is what the address lookup keys off.
    address: str = ""

    @property
    def public(self) -> bool:
        return self.id == "internet"


def _peering(presence) -> Peering | None:
    """How a tailnet session is carried, from the caller's presence; None when
    the reading names no path, and the row is left out."""

    if presence is None or presence.peer_path not in {"direct", "relayed"}:
        return None
    if presence.peer_path == "relayed":
        return Peering("relay", f"Relayed via DERP {presence.relay}")
    host, _ = split_host_port(presence.direct_endpoint)
    where = network_of(host)
    # A global IPv6 address in one of this host's own prefixes is the same
    # network, whatever its type says.
    if where == "public" and any(ip_address(host) in net for net in on_link_networks()):
        where = "network"
    if where in {"network", "loopback"}:
        return Peering("local", "Direct, on your network", address=host)
    if where == "public":
        return Peering("internet", "Direct, over the internet", address=host)
    # An endpoint in the tailnet's own range is a peer relay: another node
    # carries the link.
    return Peering("indirect", "Peer relay", address=host)


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
        return tuple(layer for layer in self.layers if layer.conclusive and not layer.holds)

    @property
    def unverified(self) -> tuple[Layer, ...]:
        return tuple(layer for layer in self.layers if not layer.conclusive)

    @property
    def verdict(self) -> str:
        """The checks, "confirmed" only when every one held on evidence, "failing" when
        one did not, else "unconfirmed"."""

        if self.failing:
            return "failing"
        return "confirmed" if self.holds else "unconfirmed"

    @property
    def tone(self) -> str:
        """The dot's tone: the network's, except that the tailnet's is held
        back until every check on the way in is confirmed."""

        if self.channel.id != "tailnet" or self.verdict == "confirmed":
            return self.channel.tone
        return "attention" if self.verdict == "failing" else "neutral"

    @property
    def summary(self) -> str:
        if self.holds:
            return f"All {len(self.layers)} checks passed"
        parts = []
        if self.failing:
            parts.append(f"{len(self.failing)} failed")
        if self.unverified:
            parts.append(f"{len(self.unverified)} not confirmed")
        return dotted(*parts)

    @property
    def tally(self) -> str:
        """The summary with the checks named and counted, for the panel, where
        the path beside it counts steps ("6 of 7 steps confirmed") and a bare
        "3 failed" reads as three of those."""

        if self.holds:
            return self.summary
        return f"{counted(len(self.layers), 'check')}, {self.summary}"

    @property
    def transport(self) -> str:
        return encryption(self.channel, self.secure_transport)

    @property
    def transport_path(self) -> str:
        """Which segment each encrypted transport protects."""

        if self.forwarded and self.channel.id == "tailnet" and self.secure_transport:
            if self.local_forwarder:
                return "WireGuard + TLS to HQ's machine"
            return dotted(f"TLS to {self.forwarder_name or 'proxy'}", "WireGuard to HQ")
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
        """Your device's name; behind a proxy, that it hides the device; else
        empty, and the address stands alone."""

        peer = self.caller_device
        if peer:
            return peer.label
        return "Behind a proxy" if self.forwarded else ""

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
        """The tailnet leg in a word or two, as Tailscale names it; empty
        without a reading. An idle link is connected: this request arrived
        over it."""

        if self.presence is None:
            return ""
        if self.path == "idle":
            return "Connected"
        return "Direct" if self.path == "direct" else f"Relayed via DERP {self.presence.relay}"

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

        return bool(self.serves_verified and self.observer and self.serves and self.observer.name == self.serves.name)

    @property
    def measurement_label(self) -> str:
        if self.link_observed_by_hq:
            return f"HQ's own machine ({self.observer.label})"
        if self.observer:
            return f"Another tailnet machine ({self.observer.label})"
        return "Not measured"

    @property
    def handshake(self) -> str:
        """When the two keys last shook hands: an age while the reading is
        current, and otherwise the age it had when HQ read the tailnet, since
        a link idle since then says nothing about now."""

        if self.presence is None:
            return MISSING
        if self.tailnet_current or not moment(self.presence.last_handshake):
            return self.presence.handshake
        age = ago(self.presence.last_handshake, now=self.tailnet_observed_at)
        if age == "just now":
            return "At the read"
        return f"{age.removesuffix(' ago')} before the read"

    @property
    def carried(self) -> str:
        if self.presence is None:
            return ""
        return dotted(f"{human_bytes(self.presence.rx_bytes)} in", f"{human_bytes(self.presence.tx_bytes)} out")

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
    def peering(self) -> Peering | None:
        """How this tailnet session is carried, or None where HQ has no device
        or no path for it, and the row is left out."""

        if self.caller_device is None:
            return None
        return _peering(self.presence)

    @property
    def tailnet_observed_at(self) -> datetime | None:
        """When the device/path evidence was last swept from Tailscale."""

        return _tailnet_observed_at(self.presence, self.observer)

    @property
    def tailnet_current(self) -> bool:
        """Whether the tailnet reading is recent enough that opening the panel
        would not ask for it again. Only then is a value read from it said as
        it is now; otherwise as it was when last read, which is no fault."""

        return _tailnet_current(self.tailnet_observed_at)


def _tailnet_observed_at(presence, observer: tailnet.Device | None) -> datetime | None:
    if presence is not None:
        return presence.observed_at
    return observer.observed_at if observer is not None else None


def _tailnet_current(observed: datetime | None) -> bool:
    """The one judgment of ``Connection.tailnet_current``, also handed to the
    admission layers, which are built before the connection exists."""

    return observed is not None and not freshness(PAGE_VISIT, observed).due


def connection(request, *, edge=None, firewall=None, principal=None) -> Connection:
    """Everything HQ can say about the request in front of it; ``principal``
    is the caller an agent's door authenticated, judged by its device's approval."""

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
    identity = identity_of(request, None if untrusted_forwarding else device, principal)

    declared = declared_machines()

    own = hq_service(request, catalog=connections.machines_once())
    serving = _serving_device_resolution(known, declared, served_at(request), machine=own.machine if own else "")
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
            tailnet_current=_tailnet_current(_tailnet_observed_at(presence, observer)),
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
    host only when the two share a machine, so the node is found by address and
    that flag is only the last resort. ``machine`` is the machine catalog's
    answer for HQ, the one every page shows.
    """

    # A device holding one of HQ's own addresses, else one holding an address
    # of the machine ``hq_machine`` places HQ on, else of ``machine``.
    for address in own_addresses(served):
        device = tailnet.device_at(address, known)
        if device is not None:
            return ServingDeviceResolution(device, verified=True, basis="found by an address on this machine")
    index = index_of(declared=declared)
    for placed, basis in (
        (hq_machine(index, (), {}, served_at=served, devices=known.values()), "found by the machine HQ is recorded on"),
        (machine, "found by the machine HQ's names lead to"),
    ):
        device = _device_on(placed, index, known)
        if device is not None:
            return ServingDeviceResolution(device, verified=True, basis=basis)
    observer = tailnet.observer(known)
    return ServingDeviceResolution(
        observer,
        verified=False,
        basis="assumed from the machine that read the tailnet" if observer else "not found",
    )


def _device_on(machine: str, index, known: dict[str, tailnet.Device]) -> tailnet.Device | None:
    """The tailnet device holding an address of ``machine``, or None."""

    if not machine:
        return None
    return next(
        (device for device in known.values() if any(index.at(address) == machine for address in device.addresses)),
        None,
    )


def _machine_name(addresses, declared) -> str:
    """The declared machine answering at any of these addresses.

    By address, because the tailnet's name for a machine is rarely the one HQ
    uses (a laptop is whatever its owner typed into it years ago) and the
    address is the one thing every source of a machine agrees on.
    """

    # The shared index over the declarations this page has already read costs
    # no query, and matches an address recorded with a port to the same one
    # recorded without.
    index = index_of(declared=declared)
    for address in addresses or ():
        name = index.at(address)
        if name:
            return name
    return ""


def _machine_url(name: str) -> str:
    return entity_link("machine", name).url
