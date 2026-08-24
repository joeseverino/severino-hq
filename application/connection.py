"""How this request reached HQ, and what had to be true for it to arrive.

Every other page here describes the estate. This one describes the reader: the
address they came from, the device that address belongs to, the person the
session says they are, and the sequence of independent things that each had to
hold before any of it got this far.

It is assembled rather than asserted. A page claiming "your connection is
secure" is decoration -- it says the same words when the gate is switched off,
when the policy has been loosened, and when the request arrived from a coffee
shop. So every line below is read from something that would change if the fact
changed: the settings the middleware actually enforces, the backends actually
installed, the tailnet's own account of who this device is and how its traffic
is being carried, and the access policy as Tailscale evaluated it.

Which means a layer can report that it does *not* hold, and say so plainly.
That is the property that makes the rest worth reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as utc
from django.conf import settings
from django.utils.csp import CSP

from functools import cache
import socket

from core.network import client_ip, is_trusted_proxy, split_host_port

from . import tailnet
from .reach import network_of


@dataclass(frozen=True)
class Channel:
    """Which network the caller is on, decided by arithmetic alone.

    Cheap on purpose: this is the part the header badge needs on every page,
    and a badge that costs a query is a badge on every page that costs a query.
    """

    id: str
    label: str
    detail: str

    @property
    def private(self) -> bool:
        return self.id in {"tailnet", "network", "loopback"}


TAILNET_CHANNEL = Channel(
    "tailnet",
    "Tailnet",
    "This request arrived from an address Tailscale issues and nothing else "
    "uses, so it came over the tailnet rather than over the network it is "
    "physically attached to.",
)
NETWORK_CHANNEL = Channel(
    "network",
    "Local network",
    "This request arrived from a private address, so it came from the network "
    "HQ is on rather than across the tailnet.",
)
LOOPBACK_CHANNEL = Channel(
    "loopback", "Loopback", "This request never left the machine HQ runs on."
)
OPAQUE_CHANNEL = Channel(
    "opaque",
    "Address not passed through",
    "Every hop in the chain was a proxy HQ knows, so the address it is judging "
    "is a proxy's rather than the caller's. Whoever is asking may well be on "
    "the tailnet -- nothing here can tell, which means the network gate is "
    "checking the proxy rather than them.",
)
ELSEWHERE_CHANNEL = Channel(
    "elsewhere",
    "Unrecognised",
    "This address is in none of the ranges HQ recognises, which should not be "
    "possible while the network gate is enforced.",
)


def channel_of(address: str) -> Channel:
    """What to call the network an address is on. No database, no query.

    The ranges themselves are `reach`'s: it already decides this for the DNS
    answers a service resolves to, and an address is on the tailnet or it is
    not regardless of which surface is asking. What belongs here is only the
    wording, because these sentences are about a caller rather than a service.
    """

    return {
        "tailnet": TAILNET_CHANNEL,
        "loopback": LOOPBACK_CHANNEL,
        "network": NETWORK_CHANNEL,
        "public": ELSEWHERE_CHANNEL,
    }.get(network_of(address), ELSEWHERE_CHANNEL)


def channel_for_request(request) -> Channel:
    """The caller channel after applying the trusted-proxy decision once."""

    channel = channel_of(client_ip(request))
    return OPAQUE_CHANNEL if _chain_is_all_proxies(request) else channel


@dataclass(frozen=True)
class Layer:
    """One thing that had to hold, and what HQ can point at to say it did.

    ``holds`` is what the badge and the ordering read. ``evidence`` is the
    value it was decided from, kept separate from ``detail`` so the reasoning
    and the reading are never confused for each other -- the whole failure this
    page exists to avoid is prose that sounds like a measurement.
    """

    id: str
    label: str
    holds: bool
    detail: str
    evidence: str = ""
    # The security boundary and concrete mechanism this verdict belongs to.
    # These are emitted with the verdict so every UI can explain the same
    # decision without maintaining a second taxonomy beside it.
    boundary: str = ""
    mechanism: str = ""
    # False means the evidence source did not answer this question. That is
    # neither a pass nor a denial and must never be rendered as either one.
    conclusive: bool = True
    # The rules behind the verdict, where the thing deciding it had rules.
    rules: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if not self.conclusive:
            return "unknown"
        return "holds" if self.holds else "does-not"


@dataclass(frozen=True)
class Peering:
    """Which network the tailnet link itself is running over.

    Being on the tailnet says the traffic is encrypted and the peer is
    enrolled. It says nothing about where the packets went, and the two are
    routinely confused: a laptop in the same room and a laptop in an airport
    lounge produce an identical page everywhere else in HQ, because both are
    "on the tailnet over WireGuard".

    Tailscale already knows the difference and HQ already stores the answer --
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
    "Not established",
    "This device is on the tailnet, but no direct or relayed path is currently "
    "negotiated, so HQ cannot say which network the session is riding over.",
)
# Not the same statement, and conflating the two was the first thing this row
# got wrong. "No peering" is a fact about the device; this is a fact about
# HQ's view of it. Behind a proxy it has not been told to trust, HQ judges the
# proxy and declines to attribute the request to any device at all -- so it has
# nothing to read a peering from, which is not evidence that none exists.
PEERING_UNATTRIBUTED = Peering(
    "unattributed",
    "Not visible from here",
    "The request reached HQ through a forwarding peer it has not been told to "
    "trust, so HQ judges the proxy rather than the caller and attributes the "
    "session to no device. The tailnet peering behind that proxy is real; this "
    "deployment simply cannot see past it to report on it.",
)
# The sweep is what fills the device inventory, and an instance that has never
# run one -- a fresh development database, a deployment whose Tailscale
# connection is not configured -- resolves nothing. Saying "not established"
# there would blame the network for an empty table.
PEERING_UNOBSERVED = Peering(
    "unobserved",
    "No tailnet observation",
    "HQ holds no swept tailnet inventory, so it cannot match this address to a "
    "device or report how the session is carried. Configure the Tailscale "
    "connection, or wait for the next sweep.",
)


def _peering(device: tailnet.Device | None) -> Peering:
    if device is None:
        return PEERING_UNKNOWN
    if device.path == "relayed":
        return Peering(
            "relay",
            f"Relayed via {device.relay}" if device.relay else "Relayed",
            "The two nodes could not open a direct path to each other, so "
            "Tailscale is forwarding this session through one of its relays. "
            "The relay carries ciphertext and holds no key to it, but the "
            "traffic does cross a machine neither end owns.",
        )
    endpoint = device.direct_endpoint
    if not endpoint:
        return PEERING_UNKNOWN
    host, _ = split_host_port(endpoint)
    where = network_of(host)
    if where in {"network", "loopback"}:
        return Peering(
            "local",
            "Over your own network",
            "The two nodes negotiated a direct path on the same private "
            "network, so this session is not crossing the internet at all. "
            "WireGuard still encrypts it end to end.",
            address=host,
        )
    if where == "public":
        return Peering(
            "internet",
            "Over the public internet",
            "The two nodes negotiated a direct path across the internet, so "
            "this session is riding over it from the address below. WireGuard "
            "encrypts every packet, and nothing between the two ends can read "
            "it -- but the path is a public one.",
            address=host,
        )
    # A tailnet-range endpoint means the peering is itself being carried by
    # another tailnet hop. Rare, and worth naming rather than guessing at.
    return Peering(
        "indirect",
        "Over another tailnet hop",
        "The negotiated endpoint is itself a tailnet address, so this session "
        "is being carried by another node on the tailnet rather than by a "
        "network HQ can name.",
        address=host,
    )


@dataclass(frozen=True)
class Identity:
    """Who is asking, according to each system that independently knows."""

    username: str = ""
    email: str = ""
    full_name: str = ""
    sso_only: bool = False
    backends: tuple[str, ...] = ()
    session_expires: datetime | None = None
    tailnet_user: str = ""
    # Which backend actually signed this session in, rather than which ones are
    # installed. Django records it on the session at login, so it is the one
    # statement about this session that cannot be inferred from configuration.
    signed_in_by: str = ""
    provider: str = ""
    groups: tuple[str, ...] = ()
    staff: bool = False
    superuser: bool = False
    last_sign_in: datetime | None = None
    token_expires: str = ""

    @property
    def route(self) -> str:
        """How this session was established, in a word."""

        if not self.signed_in_by:
            return "unknown"
        return "single sign-on" if "OIDC" in self.signed_in_by else "password"

    @property
    def corroborated(self) -> bool:
        """Whether two independent systems agree on who is asking.

        The session says who signed in; the tailnet says which account owns the
        device the request came from. Neither consults the other, so agreement
        between them is worth something -- and disagreement is worth more,
        because it is the shape a stolen session would have.
        """

        return bool(self.tailnet_user) and bool(self.username or self.email)


@dataclass(frozen=True)
class Connection:
    """One request, described from the outside in."""

    address: str
    channel: Channel
    device: tailnet.Device | None
    serves: tailnet.Device | None
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
    serves_url: str = ""
    # What HQ was told about its machines, read once and answering every
    # relationship this page draws: which machine each end of the link is, and
    # which of a node's addresses are ones HQ was actually declared at.
    declared: tuple = ()
    untrusted_forwarding: bool = False

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
            return f"{len(self.layers)} of {len(self.layers)} checks hold"
        parts = []
        if self.failing:
            parts.append(f"{len(self.failing)} failed")
        if self.unverified:
            parts.append(f"{len(self.unverified)} unverified")
        return " · ".join(parts)

    @property
    def transport(self) -> str:
        """The encryption this request can actually prove."""

        if self.channel.id == "tailnet":
            return "WireGuard + TLS" if self.secure_transport else "WireGuard only"
        return "TLS" if self.secure_transport else "No verified encryption"

    @property
    def transport_path(self) -> str:
        """Which segment each encrypted transport protects."""

        if self.forwarded and self.channel.id == "tailnet" and self.secure_transport:
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
        return peer.label if peer else "Device not resolved"

    @property
    def peer_address(self) -> str:
        return self.reported_address if self.reported_device else self.address

    @property
    def path(self) -> str:
        return self.caller_device.path if self.caller_device else "unknown"

    @property
    def path_label(self) -> str:
        if self.forwarded:
            return f"Via {self.forwarder_name or 'forwarding peer'}"
        if self.caller_device is None:
            return "Unknown"
        return {
            "direct": "Direct",
            "relayed": f"Relayed via {self.caller_device.relay}",
            "idle": "Not negotiated",
        }[self.caller_device.path]

    @property
    def handshake(self) -> str:
        return _ago(self.caller_device.last_handshake) if self.caller_device else "—"

    @property
    def carried(self) -> str:
        if self.caller_device is None:
            return ""
        return (
            f"{_bytes(self.caller_device.rx_bytes)} in · "
            f"{_bytes(self.caller_device.tx_bytes)} out"
        )

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
            # that this caller is missing from it -- read from a field the
            # page already holds, so distinguishing the two costs no query.
            if self.serves is None:
                return PEERING_UNOBSERVED
            return PEERING_UNKNOWN
        return _peering(self.caller_device)

    @property
    def tailnet_observed_at(self) -> datetime | None:
        """When the device/path evidence was last swept from Tailscale."""

        return self.caller_device.observed_at if self.caller_device else None


def connection(request) -> Connection:
    """Everything HQ can say about the request in front of it."""

    address = client_ip(request)
    peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
    forwarded = bool(request.META.get("HTTP_X_FORWARDED_FOR"))
    forwarding_trusted = forwarded and is_trusted_proxy(peer)
    untrusted_forwarding = forwarded and not forwarding_trusted
    # (see `_serving_device` for why the observer flag is not the answer)
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
    identity = _identity(request, None if untrusted_forwarding else device)
    from .infrastructure import declared_machines

    declared = declared_machines()
    serves = _serving_device(known, declared)
    peer_device = reported_device if untrusted_forwarding else device
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
        machine_url=_machine_url(machine_name),
        machine_name=machine_name,
        forwarder_name=forwarder_name,
        forwarder_url=_machine_url(forwarder_name),
        forwarded=forwarded,
        serves_url=_machine_url(serves_name),
        declared=declared,
        identity=identity,
        untrusted_forwarding=untrusted_forwarding,
        secure_transport=bool(request.is_secure()),
        host=request.get_host(),
        layers=_layers(
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
        ),
    )


def displayed_client_ip(request) -> str:
    """The best caller address HQ may display without authorizing from it.

    ``client_ip`` remains the sole source for admission. When an undeclared
    proxy reports a caller, the rightmost forwarded hop may still be correlated
    for explanatory UI; keeping this function explicitly presentation-only
    prevents that useful knowledge from quietly becoming network authority.
    """

    peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
    forwarded = [
        hop.strip()
        for hop in str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",")
        if hop.strip()
    ]
    if forwarded and not is_trusted_proxy(peer):
        return split_host_port(forwarded[-1])[0]
    return client_ip(request)


def _chain_is_all_proxies(request) -> bool:
    """Whether the forwarded chain identified anybody at all."""

    peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
    forwarded = [
        split_host_port(hop.strip())[0]
        for hop in str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",")
        if hop.strip()
    ]
    return (
        bool(forwarded)
        and is_trusted_proxy(peer)
        and all(is_trusted_proxy(hop) for hop in forwarded)
    )


def _identity(request, device: tailnet.Device | None) -> Identity:
    user = getattr(request, "user", None)
    backends = tuple(getattr(settings, "AUTHENTICATION_BACKENDS", ()))
    session = getattr(request, "session", None)
    expiry = None
    if session is not None:
        try:
            expiry = session.get_expiry_date()
        except (AttributeError, ValueError, TypeError):
            expiry = None
    signed_in_by = str(
        (session or {}).get("_auth_user_backend", "") if session is not None else ""
    )
    return Identity(
        username=getattr(user, "username", "") or "",
        email=getattr(user, "email", "") or "",
        full_name=(getattr(user, "get_full_name", lambda: "")() or "").strip(),
        # Whether a password could sign anyone in at all. Read from what is
        # installed rather than from a flag saying it is not, because the
        # backend list is the thing Django actually consults.
        sso_only=bool(backends)
        and not any(name.endswith("ModelBackend") for name in backends),
        backends=backends,
        session_expires=expiry,
        tailnet_user=device.user if device else "",
        signed_in_by=signed_in_by,
        # Who vouched for the person, taken from the endpoint HQ actually sends
        # them to rather than from a name written down beside it. A configured
        # provider did not vouch for a password session, so it must not appear
        # beside that session as though it did.
        provider=_provider_host() if "OIDC" in signed_in_by else "",
        groups=tuple(
            getattr(user, "groups", None).values_list("name", flat=True)
            if getattr(user, "pk", None)
            else ()
        ),
        staff=bool(getattr(user, "is_staff", False)),
        superuser=bool(getattr(user, "is_superuser", False)),
        last_sign_in=getattr(user, "last_login", None),
        token_expires=str(
            (session or {}).get("oidc_id_token_expiration", "")
            if session is not None
            else ""
        ),
    )


def _provider_host() -> str:
    """The identity provider, named by where sign-in is actually sent."""

    from urllib.parse import urlsplit

    endpoint = str(getattr(settings, "OIDC_OP_AUTHORIZATION_ENDPOINT", "") or "")
    return urlsplit(endpoint).hostname or ""


def _layers(
    request,
    address: str,
    channel: Channel,
    device: tailnet.Device | None,
    forwarder: tailnet.Device | None,
    serves: tailnet.Device | None,
    identity: Identity,
    known: dict[str, tailnet.Device],
    *,
    forwarded: bool,
    forwarding_trusted: bool,
    forwarding_peer: str,
) -> tuple[Layer, ...]:
    """The independent things that each had to hold, outermost first.

    Ordered the way a request encounters them rather than by importance, so
    reading down the list is reading the path in. Depth is the point: no single
    one of these is the reason HQ is not on the internet.
    """

    found = [
        _name_layer(request),
        _channel_layer(address, channel),
        *_policy_layers(device, forwarder, serves, request, known, forwarded),
        _device_layer(device),
        _forwarder_layer(
            forwarder,
            trusted=forwarding_trusted,
            peer=forwarding_peer,
        )
        if forwarded
        else None,
        _gate_layer(channel),
        _sign_in_layer(identity),
        _session_layer(request, identity),
        _transport_layer(request, channel),
        _canonical_layer(),
        _browser_layer(),
    ]
    return tuple(layer for layer in found if layer is not None)


def _name_layer(request) -> Layer:
    """Whether the name in the address bar is one the internet can resolve."""

    from .zones import public_answers_for

    host = request.get_host().partition(":")[0]
    answers = public_answers_for(host)
    return Layer(
        "name",
        "The name is not published",
        not answers,
        (
            "No public DNS record for this name exists in any zone HQ manages, "
            "so resolving it from the internet returns nothing to connect to."
            if not answers
            else "A public DNS record for this name exists, so the name itself "
            "does not keep anyone away from HQ."
        ),
        evidence=host if not answers else f"{host} → {', '.join(answers)}",
        boundary="Exposure",
        mechanism="Authoritative DNS",
    )


def _channel_layer(address: str, channel: Channel) -> Layer:
    return Layer(
        "channel",
        "The address is on the tailnet",
        channel.id == "tailnet",
        channel.detail,
        evidence=address,
        boundary="Network",
        mechanism="Tailnet address space",
    )


def _policy_layers(
    device: tailnet.Device | None,
    forwarder: tailnet.Device | None,
    serves: tailnet.Device | None,
    request,
    known: dict[str, tailnet.Device],
    forwarded: bool,
) -> tuple[Layer, ...]:
    """Every independently authorized Tailnet segment in this request path.

    Being on the tailnet is not being allowed to reach everything on it, and
    that distinction is the one people assume away. Answered by Tailscale
    during the sweep rather than worked out here, for the reason given in
    ``application.tailnet``: a second implementation of an access policy is
    believed exactly as much as the real one and wrong where nobody looks.
    """

    if not forwarded:
        layer = _policy_layer(
            "policy",
            "The policy admits this device",
            device,
            serves,
            _port_of(request),
            known,
        )
        return (layer,) if layer else ()

    edge = _policy_layer(
        "edge-policy",
        "The policy admits you to the proxy",
        device,
        forwarder,
        443 if request.is_secure() else 80,
        known,
    )
    service = _policy_layer(
        "service-policy",
        "The policy admits the proxy to HQ",
        forwarder,
        serves,
        _server_port(request),
        known,
    )
    return tuple(layer for layer in (edge, service) if layer is not None)


def _policy_layer(
    layer_id: str,
    label: str,
    source: tailnet.Device | None,
    target: tailnet.Device | None,
    port: int,
    known: dict[str, tailnet.Device],
) -> Layer | None:
    if source is None or target is None:
        return None
    # Names for the lookup, labels for the sentence: the policy is keyed on the
    # node's registered name, but a person reads the MagicDNS label, and two
    # phones registered as "localhost" are indistinguishable in the other one.
    verdict = tailnet.may_reach(source.name, target.name, port, known)
    if not verdict.known:
        return Layer(
            layer_id,
            label,
            False,
            verdict.detail,
            evidence=f"{source.label} → {target.label} on {port}",
            boundary="Zero trust policy",
            mechanism="Tailscale grants",
            conclusive=False,
        )
    return Layer(
        layer_id,
        label,
        verdict.allowed,
        verdict.detail,
        evidence=", ".join(verdict.via) or f"port {port}",
        # The grant itself. "Allowed" without the rule that allowed it is a
        # verdict nobody can check, and the rule is the thing an operator would
        # go and change -- so the answer carries it rather than pointing at a
        # policy document and wishing them luck.
        rules=tuple(
            f"{' '.join(rule.get('who') or ['?'])} → "
            f"{' '.join(rule.get('to') or ['?'])}"
            for rule in verdict.rules
        ),
        boundary="Zero trust policy",
        mechanism="Tailscale grants",
    )


def _port_of(request) -> int:
    host = request.get_host()
    _, separator, port = host.rpartition(":")
    if separator and port.isdigit():
        return int(port)
    return 443 if request.is_secure() else 80


def _server_port(request) -> int:
    """The port the forwarding peer actually reached on HQ."""

    value = str(request.META.get("SERVER_PORT", "") or "")
    return int(value) if value.isdigit() else _port_of(request)


def _device_layer(device: tailnet.Device | None) -> Layer:
    if device is None:
        return Layer(
            "device",
            "The device is a known node",
            False,
            "No device on the tailnet answers at this address, so HQ cannot "
            "say which machine is asking.",
            boundary="Device identity",
            mechanism="Tailnet node inventory",
        )
    carried = (
        f"carried by the {device.relay} relay"
        if device.path == "relayed"
        else "over a direct path"
        if device.path == "direct"
        else "with no path currently negotiated"
    )
    expiry = (
        f" Its node key {_expiry_phrase(device.key_expires)}."
        if device.key_expires
        else " Its node key does not expire."
    )
    return Layer(
        "device",
        "The device is a known node",
        True,
        f"{device.label} is enrolled on the tailnet, owned by "
        f"{device.user or 'nobody in particular'}, and is talking to HQ "
        f"{carried}.{expiry}",
        evidence=device.dns_name or device.name,
        boundary="Device identity",
        mechanism="WireGuard node key",
    )


def _forwarder_layer(
    device: tailnet.Device | None, *, trusted: bool, peer: str
) -> Layer:
    if not trusted:
        return Layer(
            "forwarder",
            "The forwarding peer is explicitly trusted",
            False,
            "The socket peer is not in HQ's exact proxy allowlist, so its "
            "forwarded identity is ignored.",
            evidence=peer,
            boundary="Forwarding identity",
            mechanism="Exact proxy allowlist",
        )
    if device is None:
        return Layer(
            "forwarder",
            "The forwarding peer is explicitly trusted",
            True,
            "The socket peer is in HQ's exact proxy allowlist. A local reverse "
            "proxy does not need to be a separate Tailnet node to be trusted.",
            evidence=peer,
            boundary="Forwarding identity",
            mechanism="Exact proxy allowlist",
        )
    return Layer(
        "forwarder",
        "The forwarding peer is explicitly trusted",
        True,
        f"{device.label} owns the allowlisted Tailnet address that opened the "
        "socket to HQ.",
        evidence=device.dns_name or device.name,
        boundary="Forwarding identity",
        mechanism="Exact proxy allowlist and WireGuard node key",
    )


def _expiry_phrase(stamp: str) -> str:
    """When a node key runs out, in the tense that fits.

    An expired key is not a smaller version of a valid one -- the device stops
    being on the tailnet -- so the two do not share a sentence.
    """

    parsed = _parsed(stamp)
    if parsed is None:
        return "has an expiry HQ could not read"
    days = (parsed - datetime.now(utc.utc)).days
    if days < 0:
        return f"expired {abs(days)} days ago"
    return f"expires in {days} days"


def _gate_layer(channel: Channel) -> Layer:
    """Whether HQ refuses everything else before it authenticates anything."""

    enforced = bool(getattr(settings, "SEVERINO_ENFORCE_TRUSTED_NETWORK", False))
    if enforced and channel.id == "opaque":
        return Layer(
            "gate",
            "HQ refuses anywhere else",
            False,
            "The gate is enforced, but the address reaching it is a proxy's "
            "rather than the caller's, so it is admitting the proxy. Anyone "
            "who can reach that proxy passes this check.",
            evidence="judging a proxy",
            boundary="Application edge",
            mechanism="Pre-auth network gate",
        )
    return Layer(
        "gate",
        "HQ refuses anywhere else",
        enforced and channel.private,
        (
            "Requests from outside the ranges HQ accepts are refused before "
            "sessions, authentication or any view runs -- so an address that "
            "may not be here cannot reach the sign-in form or appear in the "
            "audit log as an attempt at anything."
            if enforced
            else "The network gate is not being enforced in this deployment, so "
            "the address a request comes from is not being checked at all."
        ),
        evidence="enforced" if enforced else "not enforced",
        boundary="Application edge",
        mechanism="Pre-auth network gate",
    )


def _sign_in_layer(identity: Identity) -> Layer:
    return Layer(
        "sign-in",
        "Only single sign-on can sign in",
        identity.sso_only,
        (
            "No password backend is installed, so there is no password to "
            "guess, reuse or leak -- signing in goes through the identity "
            "provider and nothing else."
            if identity.sso_only
            else "A password backend is installed, so a password can sign "
            "somebody in here."
        ),
        evidence=", ".join(name.rpartition(".")[2] for name in identity.backends),
        boundary="Human identity",
        mechanism="Authentication backends",
    )


def _session_layer(request, identity: Identity) -> Layer:
    """Whether the cookie carrying this session can leave where it was set."""

    secure = bool(getattr(settings, "SESSION_COOKIE_SECURE", False))
    http_only = bool(getattr(settings, "SESSION_COOKIE_HTTPONLY", False))
    same_site = str(getattr(settings, "SESSION_COOKIE_SAMESITE", "") or "")
    name = str(getattr(settings, "SESSION_COOKIE_NAME", "") or "")
    # The `__Host-` prefix is the only one of these the browser enforces
    # against somebody else. The other three describe the cookie HQ set; this
    # one is a promise no sibling host can have written it.
    prefixed = name.startswith("__Host-")
    holds = secure and http_only and bool(same_site) and prefixed
    stated = [
        f"Secure={'on' if secure else 'off'}",
        f"HttpOnly={'on' if http_only else 'off'}",
        f"SameSite={same_site or 'unset'}",
        name or "unnamed",
    ]
    return Layer(
        "session",
        "The session cannot be read, sent elsewhere, or forged by a neighbour",
        holds,
        (
            "The session cookie is not sent over plain HTTP, cannot be read by "
            "script, is not attached to requests another site starts, and "
            "carries the `__Host-` prefix -- so the browser refuses to store "
            "one of this name from any other host or path, and nothing under "
            "this domain can plant a session for HQ to read back."
            if holds
            else "The session cookie is missing at least one of the flags that "
            "keep it from being read, replayed, or set by a neighbouring host."
        ),
        evidence=" · ".join(stated),
        boundary="Session",
        mechanism="Cookie policy",
    )


def _browser_layer() -> Layer:
    """What HQ tells the browser it is allowed to do with this page.

    Every other layer on this page is about reaching HQ. This one is about what
    happens after: the page is the last place a credential is held, and the
    policy is the only boundary HQ cannot check from the inside -- it is
    enforced in someone else's browser, and a directive that has quietly
    stopped applying looks exactly like one that is quietly working. So the
    policy is stated here, and violations are reported back.
    """

    policy = dict(getattr(settings, "SECURE_CSP", {}) or {})
    trusted_types = "'script'" in (policy.get("require-trusted-types-for") or ())
    scripts = tuple(policy.get("script-src") or ())
    nonce_only = CSP.NONCE in scripts and CSP.UNSAFE_INLINE not in scripts
    framed = tuple(policy.get("frame-ancestors") or ()) == (CSP.NONE,)
    reports = bool(policy.get("report-uri") or policy.get("report-to"))
    holds = trusted_types and nonce_only and framed
    stated = [
        "Trusted Types" if trusted_types else "no Trusted Types",
        "nonce scripts" if nonce_only else "inline scripts",
        "no framing" if framed else "framing allowed",
        "reported" if reports else "unreported",
    ]
    return Layer(
        "browser",
        "The page cannot run what HQ did not send",
        holds,
        (
            "Script runs only from this origin or under a nonce minted for "
            "this one response, the page cannot be framed, and Trusted Types "
            "makes assigning a string to a DOM sink throw rather than parse -- "
            "so a cross-site scripting bug has nowhere to execute even if one "
            "is introduced. The browser reports anything it refuses, which is "
            "the only way HQ learns a directive stopped holding."
            if holds
            else "The content policy is missing at least one of the directives "
            "that keep this page from running script HQ did not send."
        ),
        evidence=" · ".join(stated),
        boundary="Browser boundary",
        mechanism="Content Security Policy",
    )


def _canonical_layer() -> Layer:
    """Whether plain HTTP is a way in, and whether the browser is told it isn't.

    HQ binds a plain port and a proxy terminates TLS in front of it, so "the
    site is HTTPS" is a fact about the proxy rather than about HQ. Two things
    make it a fact about HQ: refusing a request that did not arrive as HTTPS,
    and telling the browser never to try plain HTTP for this name again.
    """

    redirected = bool(getattr(settings, "SECURE_SSL_REDIRECT", False))
    hsts = int(getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0)
    subdomains = bool(getattr(settings, "SECURE_HSTS_INCLUDE_SUBDOMAINS", False))
    holds = redirected and hsts > 0
    stated = [
        "plain HTTP redirected" if redirected else "plain HTTP served",
        f"HSTS {hsts // 86400} days" if hsts else "HSTS not sent",
    ]
    if hsts and subdomains:
        stated.append("subdomains included")
    return Layer(
        "canonical",
        "There is one way in, and it is encrypted",
        holds,
        (
            "A request that did not arrive over TLS is sent to the canonical "
            "name, so the plain port HQ binds is not a second front door. The "
            "browser is told to refuse plain HTTP for this name from now on, "
            "which closes the one request that would otherwise be made in the "
            "clear -- the first one, before any redirect."
            if holds
            else "HQ is serving plain HTTP on the port it binds"
            if not redirected
            else "HQ redirects plain HTTP, but sends no HSTS header, so the "
            "first request a browser makes to this name can still be made in "
            "the clear before the redirect answers it."
        ),
        evidence=" · ".join(stated),
        boundary="Transport",
        mechanism="HTTPS redirect and HSTS",
    )


def _transport_layer(request, channel: Channel) -> Layer:
    tls = bool(request.is_secure())
    tailnet = channel.id == "tailnet"
    if tailnet and tls:
        detail = (
            "Tailscale encrypts the link between the two machines end to end, "
            "and TLS encrypts this request inside it. Either alone would do; "
            "neither depends on the other being sound."
        )
        evidence = "WireGuard + TLS"
    elif tailnet:
        detail = "The tailnet encrypts this request with WireGuard, without TLS inside it."
        evidence = "WireGuard only"
    elif tls:
        detail = "TLS encrypts this request, but the caller is not on the tailnet."
        evidence = "TLS"
    else:
        detail = "HQ cannot verify an encrypted transport for this request."
        evidence = "No verified encryption"
    return Layer(
        "transport",
        "The transport is encrypted",
        tls or tailnet,
        detail,
        evidence=evidence,
        boundary="Transport",
        mechanism="WireGuard and TLS",
    )


@dataclass(frozen=True)
class Address:
    """One address, what kind it is, and how HQ came to know it."""

    value: str
    kind: str
    label: str
    source: str
    current: bool = False


def _address_row(value: str, source: str, *, current: bool = False) -> Address | None:
    """An endpoint classified by the range it falls in.

    ``host:port`` and bracketed IPv6 both arrive here -- the daemon writes
    endpoints that way -- so the port is taken off before classifying and put
    back for display, because which port a path uses is part of the answer.
    """

    text = str(value or "").strip()
    if not text:
        return None
    bare = text
    if bare.startswith("["):
        bare = bare.partition("]")[0].lstrip("[")
    elif bare.count(":") == 1:
        bare = bare.rpartition(":")[0]
    channel = channel_of(bare)
    return Address(
        value=text,
        kind=channel.id,
        label={
            "tailnet": "Tailnet",
            "network": "Local network",
            "loopback": "Loopback",
        }.get(channel.id, "Public"),
        source=source,
        current=current,
    )


def addresses_of(found: Connection) -> tuple[Address, ...]:
    """Every address HQ can associate with the caller, kind by kind.

    Three different things get called "my IP" and they are rarely the same
    number: the one Tailscale issued, the one the router handed out, and the
    one the internet sees. HQ holds all three from separate places -- the
    request itself, the device record, and the path the two daemons negotiated
    -- and this is the only surface that puts them beside each other.
    """

    current = found.peer_address
    current_source = (
        "reported by the forwarding hop; not used for admission"
        if found.untrusted_forwarding
        else "this request arrived from it"
    )
    rows: list[Address | None] = [
        _address_row(current, current_source, current=True)
    ]
    device = found.caller_device
    if device is not None:
        rows.extend(
            _address_row(address, "issued to this device by Tailscale")
            for address in device.addresses
            if address != current
        )
        rows.append(
            _address_row(
                device.direct_endpoint,
                "the last Tailnet sweep observed this tunnel endpoint",
            )
        )
        rows.extend(
            _address_row(endpoint, "the last Tailnet sweep observed this endpoint")
            for endpoint in device.endpoints
        )
    return tuple(_deduplicated(rows))


@cache
def _own_addresses() -> frozenset[str]:
    """Every address this process is actually reachable at.

    Asked of the host rather than of any inventory, because it is the one fact
    about "where HQ runs" that no sweep can be wrong about. A UDP socket is
    only connected locally -- it sends no packet -- and exposes the address
    the kernel would route from without putting DNS in a request path.

    Cached for the life of the process. Where HQ runs does not change without
    a restart, and a restart is what clears this.
    """

    found = {"127.0.0.1", "::1"}
    for family, destination in (
        (socket.AF_INET, "192.0.2.1"),
        (socket.AF_INET6, "2001:db8::1"),
    ):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect((destination, 9))
                found.add(str(probe.getsockname()[0]).split("%", 1)[0])
        except OSError:
            continue
    return frozenset(found)


def _serving_device(
    known: dict[str, tailnet.Device], declared: tuple[dict[str, object], ...]
) -> tailnet.Device | None:
    """The tailnet node HQ is actually running on.

    The obvious answer -- the device the sweep marked ``self`` -- is the device
    whose *daemon took the reading*, which is the controller's host. Those are
    the same machine only when HQ and the controller share one, and they are
    not obliged to: run HQ anywhere else and it introduces itself as the
    controller's host, then evaluates the reachability verdict against the
    wrong node, confidently.

    Resolved by address, never by name: a tailnet name, an mDNS name and a
    declaration key are three strings for one machine, and matching any of them
    is how the wrong node gets picked.

    It takes two hops, because neither end holds both halves. The host knows
    the addresses it answers at -- a LAN address, typically, since a tailnet
    address lives on an interface the hostname does not resolve to. The tailnet
    knows only its own addresses. The *declaration* is the one place both are
    written down, so it is the bridge: own address -> declared machine ->
    tailnet device.

    Falls back to the observer flag when nothing resolves, which keeps a
    single-host deployment behaving exactly as it did.
    """

    mine = _own_addresses()
    if mine:
        for device in known.values():
            if mine.intersection(device.addresses):
                return device
        for machine in declared:
            addresses = frozenset(machine.get("addresses") or ())
            if not mine.intersection(addresses):
                continue
            for device in known.values():
                if addresses.intersection(device.addresses):
                    return device
    return tailnet.observer(known)


def addresses_of_hq(found: Connection) -> tuple[Address, ...]:
    """The stable tailnet addresses assigned to the device serving HQ."""

    serves = found.serves
    if serves is None:
        return ()
    rows: list[Address | None] = [
        _address_row(address, "HQ answers here") for address in serves.addresses
    ]
    return tuple(_deduplicated(rows))


def _machine_name(addresses, declared) -> str:
    """The declared machine answering at any of these addresses.

    By address, because the tailnet's name for a machine is rarely the one HQ
    uses -- a laptop is whatever its owner typed into it years ago -- and the
    address is the one thing every source of a machine agrees on.

    Resolved through the shared index, over the declarations this page has
    already read, so it costs no query. This once intersected two sets of
    strings, which meant an address recorded with a port on one side and
    without on the other failed to match a machine HQ had both halves of.
    """

    from .locate import index_of

    index = index_of(declared=declared)
    for address in addresses or ():
        name = index.at(address)
        if name:
            return name
    return ""


def _machine_url(name: str) -> str:
    if not name:
        return ""
    from django.urls import reverse

    return reverse("control_plane:machine", kwargs={"name": name})


def _deduplicated(rows) -> list[Address]:
    """One row per address, with the ports it was seen on folded into it.

    A node reports the same address on several ports, and listing each as its
    own row turns four facts into a dozen lines that all say the same thing.
    """

    found: dict[str, Address] = {}
    ports: dict[str, list[str]] = {}
    for row in rows:
        if row is None:
            continue
        host, port = split_host_port(row.value)
        if host not in found:
            found[host] = row
            ports[host] = []
        if port and port not in ports[host]:
            ports[host].append(port)
    return [
        Address(
            value=f"{host}:{'/'.join(ports[host])}" if ports[host] else host,
            kind=row.kind,
            label=row.label,
            source=row.source,
            current=row.current,
        )
        for host, row in found.items()
    ]


@dataclass(frozen=True)
class Header:
    """One header as it arrived, and what HQ did with it."""

    name: str
    value: str
    purpose: str = ""
    # Why this one is not believed, where declining it was a decision rather
    # than an absence.
    declined: str = ""
    redacted: bool = False

    @property
    def used(self) -> bool:
        return bool(self.purpose)

    @property
    def state(self) -> str:
        if self.purpose:
            return "read"
        return "declined" if self.declined else "ignored"


# What each header HQ reads is read *for*. Written here rather than inferred,
# because the thing being described is what the code does with it -- and a
# header nobody reads has no entry, which is the point of the second list.
HEADERS_READ = {
    "Host": "Which site this is, checked against the hosts HQ will answer for.",
    "X-Forwarded-For": "The chain HQ walks to decide the address it judges you by.",
    "X-Forwarded-Proto": "Whether the proxy terminated TLS, so HQ knows the request was encrypted.",
    "Origin": "Checked against the origins allowed to submit forms here.",
    "Referer": "The same check, for browsers that send this instead.",
    "Cookie": "Carries the session. Its contents are never shown, here or anywhere.",
}
REDACTED = {"Cookie", "Authorization", "X-Csrftoken", "Proxy-Authorization"}
# Headers deliberately not believed, and the reason. Without these the page
# lists a header carrying the correct answer as merely ignored, which reads as
# an oversight rather than as the safer of two choices.
HEADERS_DECLINED = {
    "X-Real-Ip": (
        "Carries one address the proxy asserts, with no chain behind it to "
        "check. HQ reads the forwarded chain instead, which it can walk back "
        "through the proxies it knows and stop at the first hop it cannot "
        "vouch for. Believing a single asserted value would be weaker."
    ),
    "X-Forwarded-Scheme": (
        "Says the same thing as X-Forwarded-Proto, which is the one Django is "
        "configured to read. Two sources for one fact is one more than can be "
        "trusted to agree."
    ),
    "X-Forwarded-Host": (
        "The host is taken from the request line and checked against the hosts "
        "HQ will answer for. A forwarded copy could disagree with it."
    ),
}


def headers_of(request) -> tuple[Header, ...]:
    """Every header this request carried, with the ones HQ acts on first.

    The raw input to every decision on this page. A value HQ reads is worth
    seeing beside the conclusion drawn from it -- and the ones it does *not*
    read are worth seeing too, because "the proxy is sending X-Real-IP and
    nothing here looks at it" is invisible until somebody prints both lists.
    """

    found: list[Header] = []
    for key, value in sorted(request.META.items()):
        if key == "HTTP_HOST":
            name = "Host"
        elif key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
        else:
            continue
        redacted = name in REDACTED
        found.append(
            Header(
                name=name,
                value=(
                    f"present, {len(str(value))} characters"
                    if redacted
                    else str(value)
                ),
                purpose=HEADERS_READ.get(name, ""),
                declined=HEADERS_DECLINED.get(name, ""),
                redacted=redacted,
            )
        )
    # Acted on first, then deliberately declined, then everything else.
    order = {"read": 0, "declined": 1, "ignored": 2}
    return tuple(sorted(found, key=lambda header: order[header.state]))


@dataclass(frozen=True)
class Hop:
    """One address in the chain, and what HQ decided about it."""

    value: str
    role: str
    detail: str


def hops_of(request) -> tuple[Hop, ...]:
    """How HQ arrived at the address it is judging this request by.

    The most quietly consequential decision on the page. Behind a proxy every
    request arrives from the proxy, and the caller's address is in a header
    anyone can write -- so which hop HQ believes is the whole of whether the
    network gate means anything. Showing the working is how a misconfigured
    proxy list becomes visible instead of silently trusting a stranger.
    """

    from core.network import is_trusted_proxy

    peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
    forwarded = [
        hop.strip()
        for hop in str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",")
        if hop.strip()
    ]
    judged = client_ip(request)
    if not forwarded or not is_trusted_proxy(peer):
        return (
            Hop(
                peer,
                "judged",
                "The socket peer is not in HQ's proxy allowlist, so any "
                "forwarded client address is ignored."
                if forwarded
                else "The socket peer is the caller address HQ evaluates.",
            ),
        )
    # Walked right to left, the way it is decided: from the hop the trusted
    # proxy observed, discarding proxies HQ knows, stopping at the first it
    # does not. Everything left of that is text a caller can choose.
    chain = [*forwarded, peer]
    # Decided right to left, but read left to right, which is the order the
    # hops actually occurred in. Walking one way and printing the other is how
    # this ends up looking like the answer came from the wrong end.
    roles: dict[int, str] = {}
    settled = False
    for index in range(len(chain) - 1, -1, -1):
        if settled:
            roles[index] = "ignored"
        elif is_trusted_proxy(chain[index]):
            roles[index] = "proxy"
        else:
            roles[index] = "judged"
            settled = True
    detail = {
        "proxy": (
            "Local reverse proxy. HQ accepts client-address headers only from "
            "this exact socket peer."
        ),
        "judged": "The closest address to the caller that HQ can prove.",
        "ignored": (
            "Further from the connection than the address HQ settled on, so it "
            "is text a caller could have written. Not believed."
        ),
    }
    found = [
        Hop(value, roles[index], detail[roles[index]])
        for index, value in enumerate(chain)
    ]
    if not settled:
        # Every hop was a known proxy, so the peer is as close as this gets --
        # and nothing in the chain identified the caller at all.
        found[-1] = Hop(
            judged,
            "judged",
            "Every address in the chain is in HQ's proxy allowlist, so no "
            "distinct caller address was supplied. HQ evaluates the socket peer.",
        )
    return tuple(found)


def _ago(stamp: str) -> str:
    """A provider's timestamp as an age, or as the fact that there is none.

    What is local here is reading a stamp that spells "never" as the zero time
    and one that has not happened yet. The phrasing is `ui.ago`, so this reads
    the same as every other elapsed time in HQ.
    """

    from .ui import ago

    parsed = _parsed(stamp)
    if parsed is None:
        return "—"
    if parsed > datetime.now(utc.utc):
        return "just now"
    return ago(parsed)


def _parsed(stamp: str) -> datetime | None:
    text = str(stamp or "").strip()
    if not text or text.startswith("0001-01-01"):
        # Tailscale writes the zero time for "never", which as an age would
        # read as two thousand years and look like a bug rather than a fact.
        return None
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=utc.utc)


def _bytes(count: int) -> str:
    size = float(count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
