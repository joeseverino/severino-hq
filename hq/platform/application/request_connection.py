"""How this request reached HQ, and what had to be true for it to arrive.

It describes the reader: their address, device and person, and each check of
the request standard (``request_standard``) that had to hold for the request
to get this far. Every line is read from something that would change if the
fact changed (the settings the middleware enforces, the installed backends,
the tailnet, the policy as Tailscale evaluated it), so a check can say it is
not met, where "secure" would stay with the gate off.
"""

from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from hq.platform.core.network import split_host_port

from . import tailnet
from .labels import dotted, human_bytes
from .reach import network_of, on_link_networks
from .request_channel import Channel, encryption, forwarding_of
from .request_identity import Identity, identity_of
from .request_standard import RequestFacts, ServingDeviceResolution, measure_request
from .standards import CheckResult, Measurement, Standing
from .tone import Tone
from .ui import MISSING, counted
lazy from . import connections
lazy from .entity_links import entity_link
lazy from .hq_self import hq_machine, hq_service, own_addresses, served_at
lazy from .infrastructure import declared_machines
lazy from .locate import index_of
lazy from .moments import ago, moment
lazy from .tailnet_presence import tailnet_presence


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
    """One request, described from the outside in: what HQ read of it, and
    that read measured against the request standard."""

    facts: RequestFacts
    checks: Measurement
    # The machine pages behind the two ends, where HQ knows a machine at the
    # address each one answers at. A tailnet name is rarely the name HQ uses,
    # so this is resolved by address rather than by matching the two names.
    machine_url: str = ""
    machine_name: str = ""
    forwarder_name: str = ""
    forwarder_url: str = ""
    local_forwarder: bool = False
    serves_url: str = ""

    @property
    def address(self) -> str:
        return self.facts.address

    @property
    def channel(self) -> Channel:
        return self.facts.channel

    @property
    def device(self) -> tailnet.Device | None:
        return self.facts.device

    @property
    def serves(self) -> tailnet.Device | None:
        return self.facts.serves

    @property
    def observer(self) -> tailnet.Device | None:
        return self.facts.observer

    @property
    def identity(self) -> Identity:
        return self.facts.identity

    @property
    def reported_device(self) -> tailnet.Device | None:
        return self.facts.reported_device

    @property
    def forwarded(self) -> bool:
        return self.facts.forwarded

    @property
    def untrusted_forwarding(self) -> bool:
        return self.facts.untrusted_forwarding

    @property
    def serving_basis(self) -> str:
        return self.facts.serving.basis

    @property
    def presence(self) -> Any:
        return self.facts.presence

    @property
    def host(self) -> str:
        return self.facts.host

    @property
    def declared(self) -> tuple:
        return self.facts.declared

    @property
    def failing(self) -> tuple[CheckResult, ...]:
        """The checks not met that count against the request: a check of HQ's
        standard is an open item, shown quietly, unless Advanced HQ Posture is on."""

        return tuple(result for result in self.checks.unmet if result.enforced)

    @property
    def unverified(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.checks.results if result.state == Standing.UNAVAILABLE)

    @property
    def holds(self) -> bool:
        return not self.failing and not self.unverified

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
        return Tone.ATTENTION if self.verdict == "failing" else Tone.NEUTRAL

    @property
    def summary(self) -> str:
        """How the checks stand; one left open quietly is counted, never failed."""

        total = len(self.checks.results)
        if self.holds:
            return (
                f"All {total} checks passed"
                if self.checks.met == total
                else f"{self.checks.met} of {total} checks passed"
            )
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
        return f"{counted(len(self.checks.results), 'check')}, {self.summary}"

    @property
    def transport(self) -> str:
        return encryption(self.channel, self.facts.secure)

    @property
    def transport_path(self) -> str:
        """Which segment each encrypted transport protects."""

        secure = self.facts.secure
        if self.forwarded and self.channel.id == "tailnet" and secure:
            if self.local_forwarder:
                return "WireGuard + TLS to HQ's machine"
            return dotted(f"TLS to {self.forwarder_name or 'proxy'}", "WireGuard to HQ")
        if self.channel.id == "tailnet" and secure:
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
        return self.facts.reported_address if self.reported_device else self.address

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

        return bool(
            self.facts.serving.verified and self.observer and self.serves and self.observer.name == self.serves.name
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
        return self.facts.tailnet_observed_at

    @property
    def tailnet_current(self) -> bool:
        return self.facts.tailnet_current


def request_facts(request, *, edge=None, firewall=None, principal=None) -> RequestFacts:
    """What HQ reads about the request in front of it, once; ``principal``
    is the caller an agent's door authenticated."""

    forwarding = forwarding_of(request)
    address, peer, untrusted_forwarding = forwarding.client, forwarding.peer, forwarding.untrusted
    # One read of the sweep, answering every question asked of it below.
    known = tailnet.devices()
    device = tailnet.device_at(address, known)
    reported_address = forwarding.reported if untrusted_forwarding else ""
    reported_device = tailnet.device_at(reported_address, known)
    declared = declared_machines()
    own = hq_service(request, catalog=connections.machines_once())
    peer_device = reported_device if untrusted_forwarding else device
    return RequestFacts(
        request=request,
        address=address,
        # A chain that never named the caller is its own answer, and a more
        # useful one than the class its last proxy happens to fall in.
        # Reporting "local network" here would describe the proxy and read as
        # a fact about the person, which is the one confusion this page
        # exists to prevent.
        channel=forwarding.channel,
        identity=identity_of(request, None if untrusted_forwarding else device, principal),
        host=request.get_host(),
        secure=bool(request.is_secure()),
        forwarding=forwarding,
        known=known,
        device=device,
        forwarder=tailnet.device_at(peer, known) if forwarding.forwarded else None,
        reported_device=reported_device,
        reported_address=reported_address,
        # (see `_serving_device_resolution` for why the observer flag is not the answer)
        serving=_serving_device_resolution(known, declared, served_at(request), machine=own.machine if own else ""),
        observer=tailnet.observer(known),
        presence=tailnet_presence().get(peer_device.name) if peer_device else None,
        lock=tailnet.policy().lock if device is not None else {},
        declared=declared,
        edge=edge,
        firewall=firewall,
    )


def connection(request, *, edge=None, firewall=None, principal=None) -> Connection:
    """Everything HQ can say about the request in front of it, judged by
    ``principal``'s device approval when an agent's door authenticated it."""

    facts = request_facts(request, edge=edge, firewall=firewall, principal=principal)
    peer_device = facts.reported_device if facts.untrusted_forwarding else facts.device
    machine_name = _machine_name(peer_device.addresses if peer_device else (), facts.declared)
    forwarder_name = _machine_name((facts.peer,), facts.declared) if facts.forwarded else ""
    serves = facts.serves
    return Connection(
        facts,
        measure_request(facts),
        machine_url=_machine_url(machine_name),
        machine_name=machine_name,
        forwarder_name=forwarder_name,
        forwarder_url=_machine_url(forwarder_name),
        local_forwarder=facts.forwarded and network_of(facts.peer) == "loopback",
        serves_url=_machine_url(_machine_name(serves.addresses if serves else (), facts.declared)),
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
