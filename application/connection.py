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

from core.network import client_ip, split_host_port

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
    # The rules behind the verdict, where the thing deciding it had rules.
    rules: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return "holds" if self.holds else "does-not"


@dataclass(frozen=True)
class Identity:
    """Who is asking, according to each system that independently knows."""

    username: str = ""
    email: str = ""
    full_name: str = ""
    sso_only: bool = False
    backends: tuple[str, ...] = ()
    session_expires: str = ""
    tailnet_user: str = ""
    # Which backend actually signed this session in, rather than which ones are
    # installed. Django records it on the session at login, so it is the one
    # statement about this session that cannot be inferred from configuration.
    signed_in_by: str = ""
    provider: str = ""
    groups: tuple[str, ...] = ()
    staff: bool = False
    superuser: bool = False
    last_sign_in: str = ""
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
    layers: tuple[Layer, ...] = ()
    secure_transport: bool = False
    host: str = ""
    # The machine pages behind the two ends, where HQ knows a machine at the
    # address each one answers at. A tailnet name is rarely the name HQ uses,
    # so this is resolved by address rather than by matching the two names.
    machine_url: str = ""
    serves_url: str = ""
    # What HQ was told about its machines, read once and answering every
    # relationship this page draws: which machine each end of the link is, and
    # which of a node's addresses are ones HQ was actually declared at.
    declared: tuple = ()

    @property
    def holds(self) -> bool:
        return all(layer.holds for layer in self.layers)

    @property
    def failing(self) -> tuple[Layer, ...]:
        return tuple(layer for layer in self.layers if not layer.holds)

    @property
    def summary(self) -> str:
        if self.holds:
            return f"{len(self.layers)} of {len(self.layers)} checks hold"
        return f"{len(self.failing)} of {len(self.layers)} checks do not hold"

    @property
    def transport(self) -> str:
        """Both encryptions named, since the point is that there are two."""

        return "WireGuard + TLS" if self.secure_transport else "WireGuard only"

    @property
    def path(self) -> str:
        return self.device.path if self.device else "unknown"

    @property
    def path_label(self) -> str:
        if self.device is None:
            return "Unknown"
        return {
            "direct": "Direct",
            "relayed": f"Relayed via {self.device.relay}",
            "idle": "Not negotiated",
        }[self.device.path]

    @property
    def handshake(self) -> str:
        return _ago(self.device.last_handshake) if self.device else "—"

    @property
    def carried(self) -> str:
        if self.device is None:
            return ""
        return f"{_bytes(self.device.rx_bytes)} in · {_bytes(self.device.tx_bytes)} out"


def connection(request) -> Connection:
    """Everything HQ can say about the request in front of it."""

    address = client_ip(request)
    channel = channel_of(address)
    # A chain that never named the caller is its own answer, and a more useful
    # one than the class its last proxy happens to fall in. Reporting "local
    # network" here would describe the proxy and read as a fact about the
    # person, which is the one confusion this page exists to prevent.
    if _chain_is_all_proxies(request):
        channel = OPAQUE_CHANNEL
    # One read of the sweep, answering every question asked of it below.
    known = tailnet.devices()
    device = tailnet.device_at(address, known)
    serves = tailnet.observer(known)
    identity = _identity(request, device)
    from .infrastructure import declared_machines

    declared = declared_machines()
    return Connection(
        address=address,
        channel=channel,
        device=device,
        serves=serves,
        machine_url=_machine_url(device.addresses if device else (address,), declared),
        serves_url=_machine_url(serves.addresses if serves else (), declared),
        declared=declared,
        identity=identity,
        secure_transport=bool(request.is_secure()),
        host=request.get_host(),
        layers=_layers(request, address, channel, device, serves, identity, known),
    )


def _chain_is_all_proxies(request) -> bool:
    """Whether the forwarded chain identified anybody at all."""

    return any(hop.role == "judged" and "Nothing here identifies" in hop.detail
               for hop in hops_of(request))


def _identity(request, device: tailnet.Device | None) -> Identity:
    user = getattr(request, "user", None)
    backends = tuple(getattr(settings, "AUTHENTICATION_BACKENDS", ()))
    session = getattr(request, "session", None)
    expiry = ""
    if session is not None:
        try:
            expiry = session.get_expiry_date().isoformat()
        except (AttributeError, ValueError, TypeError):
            expiry = ""
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
        signed_in_by=str(
            (session or {}).get("_auth_user_backend", "") if session is not None else ""
        ),
        # Who vouched for the person, taken from the endpoint HQ actually sends
        # them to rather than from a name written down beside it.
        provider=_provider_host(),
        groups=tuple(
            getattr(user, "groups", None).values_list("name", flat=True)
            if getattr(user, "pk", None)
            else ()
        ),
        staff=bool(getattr(user, "is_staff", False)),
        superuser=bool(getattr(user, "is_superuser", False)),
        last_sign_in=(
            getattr(user, "last_login", None).isoformat()
            if getattr(user, "last_login", None)
            else ""
        ),
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
    serves: tailnet.Device | None,
    identity: Identity,
    known: dict[str, tailnet.Device],
) -> tuple[Layer, ...]:
    """The independent things that each had to hold, outermost first.

    Ordered the way a request encounters them rather than by importance, so
    reading down the list is reading the path in. Depth is the point: no single
    one of these is the reason HQ is not on the internet.
    """

    found = [
        _name_layer(request),
        _channel_layer(address, channel),
        _policy_layer(device, serves, request, known),
        _device_layer(device),
        _gate_layer(channel),
        _sign_in_layer(identity),
        _session_layer(request, identity),
        _transport_layer(request),
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
    )


def _channel_layer(address: str, channel: Channel) -> Layer:
    return Layer(
        "channel",
        "The address is on the tailnet",
        channel.id == "tailnet",
        channel.detail,
        evidence=address,
    )


def _policy_layer(
    device: tailnet.Device | None,
    serves: tailnet.Device | None,
    request,
    known: dict[str, tailnet.Device],
) -> Layer | None:
    """Whether the tailnet's own policy admits this device to this port.

    Being on the tailnet is not being allowed to reach everything on it, and
    that distinction is the one people assume away. Answered by Tailscale
    during the sweep rather than worked out here, for the reason given in
    ``application.tailnet``: a second implementation of an access policy is
    believed exactly as much as the real one and wrong where nobody looks.
    """

    if device is None or serves is None:
        return None
    port = _port_of(request)
    verdict = tailnet.may_reach(device.name, serves.name, port, known)
    if not verdict.known:
        return Layer(
            "policy",
            "The policy admits this device",
            False,
            verdict.detail,
            evidence=f"{device.name} → {serves.name} on {port}",
        )
    return Layer(
        "policy",
        "The policy admits this device",
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
    )


def _port_of(request) -> int:
    host = request.get_host()
    _, separator, port = host.rpartition(":")
    if separator and port.isdigit():
        return int(port)
    return 443 if request.is_secure() else 80


def _device_layer(device: tailnet.Device | None) -> Layer:
    if device is None:
        return Layer(
            "device",
            "The device is a known node",
            False,
            "No device on the tailnet answers at this address, so HQ cannot "
            "say which machine is asking.",
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
    )


def _session_layer(request, identity: Identity) -> Layer:
    """Whether the cookie carrying this session can leave where it was set."""

    secure = bool(getattr(settings, "SESSION_COOKIE_SECURE", False))
    http_only = bool(getattr(settings, "SESSION_COOKIE_HTTPONLY", False))
    same_site = str(getattr(settings, "SESSION_COOKIE_SAMESITE", "") or "")
    holds = secure and http_only and bool(same_site)
    stated = [
        f"Secure={'on' if secure else 'off'}",
        f"HttpOnly={'on' if http_only else 'off'}",
        f"SameSite={same_site or 'unset'}",
    ]
    return Layer(
        "session",
        "The session cannot be read or sent elsewhere",
        holds,
        (
            "The session cookie is not sent over plain HTTP, cannot be read by "
            "script, and is not attached to requests another site starts."
            if holds
            else "The session cookie is missing at least one of the flags that "
            "keep it from being read or replayed."
        ),
        evidence=" · ".join(stated),
    )


def _transport_layer(request) -> Layer:
    return Layer(
        "transport",
        "The connection is encrypted twice",
        bool(request.is_secure()),
        (
            "Tailscale encrypts the link between the two machines end to end, "
            "and TLS encrypts this request inside it. Either alone would do; "
            "neither depends on the other being sound."
            if request.is_secure()
            else "This request is not over TLS, so only the tailnet is "
            "encrypting it."
        ),
        evidence="WireGuard + TLS" if request.is_secure() else "WireGuard only",
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

    rows: list[Address | None] = [
        _address_row(found.address, "this request arrived from it", current=True)
    ]
    device = found.device
    if device is not None:
        rows.extend(
            _address_row(address, "issued to this device by Tailscale")
            for address in device.addresses
            if address != found.address
        )
        rows.append(
            _address_row(
                device.direct_endpoint,
                "the path HQ's node is using to reach this device",
            )
        )
        rows.extend(
            _address_row(endpoint, "this device reports answering here")
            for endpoint in device.endpoints
        )
    return tuple(_deduplicated(rows))


def addresses_of_hq(found: Connection) -> tuple[Address, ...]:
    """Where HQ answers, and where its daemon merely negotiates a tunnel.

    Two different facts that a single list of addresses will be read as one.
    The tailnet addresses are where HQ serves; the endpoints beside them are
    what Tailscale advertises so two daemons can find a path through NAT, and
    a public one among them is not a service on the internet -- it is the
    outside of a router, carrying WireGuard and nothing else. Printed together
    without saying which is which, that reads as HQ being on the internet.

    A node also reports every bridge gateway a container runtime gave it, which
    is a dozen rows of the same fact and no way to reach anything. Those fall
    away by keeping only private addresses HQ was declared at, so nothing here
    has to recognise a bridge.
    """

    serves = found.serves
    if serves is None:
        return ()
    declared = _declared_addresses(serves.name, found.declared)
    rows: list[Address | None] = [
        _address_row(address, "HQ answers here") for address in serves.addresses
    ]
    # The addresses HQ was declared at, where the tailnet also reports reaching
    # the node there. Not the rest of what the daemon advertises: those are
    # endpoints for finding a path through NAT, one of them the outside of a
    # router, and none of them anything HQ serves on. Printed under a heading
    # about where HQ answers, a public one of those says HQ is on the internet.
    for endpoint in serves.endpoints:
        row = _address_row(endpoint, "HQ answers here")
        if row is None or split_host_port(row.value)[0] not in declared:
            continue
        rows.append(row)
    return tuple(_deduplicated(rows))


def _machine_url(addresses, declared) -> str:
    """The page for the machine answering at any of these addresses.

    By address, because the tailnet's name for a machine is rarely the one HQ
    uses -- a laptop is whatever its owner typed into it years ago -- and the
    address is the one thing every source of a machine agrees on.
    """

    from django.urls import reverse

    wanted = {str(address) for address in addresses or ()}
    if not wanted:
        return ""
    for machine in declared:
        name = str(machine.get("name", ""))
        if name and wanted & {str(a) for a in machine.get("addresses") or ()}:
            return reverse("control_plane:machine", kwargs={"name": name})
    return ""


def _declared_addresses(name: str, declared) -> frozenset[str]:
    """Every address HQ was told this machine answers at."""

    return frozenset(
        str(address)
        for machine in declared
        if str(machine.get("name", "")) == name
        for address in machine.get("addresses") or ()
    )


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
                "The machine that actually connected. No forwarded header is "
                "being believed, because this peer is not a proxy HQ was told "
                "about."
                if forwarded
                else "The machine that actually connected, and the only "
                "address involved.",
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
        "proxy": "A proxy HQ was told to believe, so not taken as the caller.",
        "judged": "The closest address to the caller that HQ can prove.",
        "ignored": (
            "Further from the connection than the address HQ settled on, so it "
            "is text a caller could have written. Not believed."
        ),
    }
    found = [
        Hop(value, roles[index], _hop_detail(detail[roles[index]], value, index, chain))
        for index, value in enumerate(chain)
    ]
    if not settled:
        # Every hop was a known proxy, so the peer is as close as this gets --
        # and nothing in the chain identified the caller at all.
        found[-1] = Hop(
            judged,
            "judged",
            "Every hop in the chain was a proxy HQ knows, so the machine that "
            "connected is as close to the caller as this request gets. Nothing "
            "here identifies who is actually calling.",
        )
    return tuple(found)


def _hop_detail(detail: str, value: str, index: int, chain: list[str]) -> str:
    """The line for one hop, with what is worth adding about the last one.

    A loopback peer is worth saying out loud rather than filing as one more
    proxy: it means the proxy handed the request over without it crossing a
    network at all, so there is no segment between the two for anything to sit
    on. Any other peer is simply the machine that connected.
    """

    if index != len(chain) - 1:
        return detail
    if channel_of(split_host_port(value)[0]).id == "loopback":
        return (
            detail
            + " It reached HQ over loopback, so the request never crossed a "
            "network between the proxy and here."
        )
    return detail + " It is the machine that connected."



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
