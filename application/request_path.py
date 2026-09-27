"""How this request reached HQ: HQ's own path, each hop joined to the request.

``paths.hq_path(request)`` walks the path from the readings and calls
``joined`` here. Each hop is then compared with what the request itself shows:
the address it came from, the proxy that forwarded it, the forwarded chain, the
name it asked for, an Access assertion. A hop both agree on is proven by this
request; one they disagree on is a finding with the step that fixes it; one the
request cannot show says why. The admission layers of ``connection`` are
attached to the hop they decide.

``request_path(request)`` is the one projection the connection page, the API
and MCP read.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from control_plane.names import is_hostname, normalized_hostname
from control_plane.providers import CONNECTION_LABELS, PROVIDERS, TAILNET_KIND
from core.network import is_trusted_proxy, split_host_port

from .connection import Address, Connection, Header, forwarded_chain, socket_peer
from .entity_links import entity_link
from .paths import NETWORK_LABELS, Hop, Route, ServicePath, Source, why_unread
from .reach import network_of

PROVEN = "proven"
CONTRADICTED = "contradicted"
UNPROVEN = "unproven"

_CHECK_LABELS = {
    PROVEN: "Proven by this request",
    CONTRADICTED: "Contradicted by this request",
    UNPROVEN: "Not shown by this request",
}
# The layer class each check state is drawn with.
_CHECK_STATES = {PROVEN: "holds", CONTRADICTED: "does-not", UNPROVEN: "unknown"}

# The hop each admission layer is decided at. A layer whose hop is not on the
# path is shown at HQ, the hop every path ends at.
LAYER_STEPS = {
    "name": "dns",
    "edge": "ingress",
    "channel": "network",
    "arrival": "machine",
    "policy": "network",
    "edge-policy": "network",
    "service-policy": "ingress",
    "device": "device",
    "tailnet-lock": "device",
    "identity-agreement": "device",
    "forwarder": "ingress",
    "proxy-evidence": "ingress",
    "tailnet-observer": "network",
    "transport": "network",
    "gate": "hq",
    "sign-in": "hq",
    "session": "hq",
    "canonical": "hq",
    "browser": "hq",
}

# How each role in the forwarded chain was decided.
_CHAIN_DETAIL = {
    "proxy": (
        "Local reverse proxy. HQ accepts client-address headers only from this "
        "exact socket peer."
    ),
    "judged": "The closest address to the caller that HQ can prove.",
    "ignored": (
        "Further from the connection than the address HQ settled on, so it is "
        "text a caller could have written. Not believed."
    ),
}


@dataclass(frozen=True)
class Check:
    """Whether the request agrees with what the readings say about one hop."""

    state: str
    detail: str
    # The operator step that resolves a contradiction.
    step: str = ""

    @property
    def label(self) -> str:
        return _CHECK_LABELS[self.state]

    @property
    def layer_state(self) -> str:
        return _CHECK_STATES[self.state]


@dataclass(frozen=True)
class Evidence:
    """One thing the request itself carried, at the hop it belongs to."""

    label: str
    value: str
    detail: str = ""
    # For an address in the forwarded chain: judged, proxy or ignored.
    role: str = ""


# ----- The forwarded chain ----------------------------------------------------


def address_chain(request) -> tuple[Evidence, ...]:
    """How HQ arrived at the address it judges this request by, hop by hop.

    Behind a proxy every request arrives from the proxy, and the caller's
    address is in a header anyone can write, so which hop HQ believes decides
    whether the network gate means anything. Read left to right, the order the
    hops occurred in; decided right to left, from the socket peer.
    """

    from core.network import client_ip

    peer = socket_peer(request)
    forwarded = forwarded_chain(request)
    if not forwarded or not is_trusted_proxy(peer):
        return (
            _address(
                peer,
                "judged",
                "The socket peer is not in HQ's proxy allowlist, so any forwarded "
                "client address is ignored."
                if forwarded
                else "The socket peer is the caller address HQ evaluates.",
            ),
        )
    chain = [*forwarded, peer]
    roles = _roles(chain)
    found = [_address(value, roles[index], _CHAIN_DETAIL[roles[index]]) for index, value in enumerate(chain)]
    if "judged" not in roles.values():
        found[-1] = _address(
            client_ip(request),
            "judged",
            "Every address in the chain is in HQ's proxy allowlist, so no distinct "
            "caller address was supplied. HQ evaluates the socket peer.",
        )
    return tuple(found)


def _roles(chain: list[str]) -> dict[int, str]:
    """Each chain position's role, walked from the socket peer inward."""

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
    return roles


def _address(value: str, role: str, detail: str) -> Evidence:
    return Evidence("Address", value, detail, role)


# ----- The caller -------------------------------------------------------------


def caller_hop(found: Connection) -> Hop:
    """The device that made the request, as the tailnet reading names it."""

    device = found.caller_device
    link = entity_link("machine", found.machine_name) if found.machine_name else None
    if device is None:
        return Hop(
            "device",
            "Device",
            found.machine_name,
            link,
            detail=found.peer_address,
            unread=_device_gap(found),
        )
    presence = found.presence
    source = Source(
        TAILNET_KIND,
        presence.connection_ref if presence is not None else "",
        device.observed_at,
    )
    return Hop("device", "Device", device.label, link, detail=_whose(found), source=source)


def _whose(found: Connection) -> str:
    device = found.caller_device
    if found.identity.corroborated and not found.untrusted_forwarding:
        owner = "yours"
    else:
        owner = f"owned by {device.user}" if device.user else "no reported owner"
    return " · ".join(part for part in (device.os, owner) if part)


def _device_gap(found: Connection) -> str:
    label = Source(TAILNET_KIND).label
    if found.untrusted_forwarding:
        return (
            f"not read: {label}, because a proxy HQ does not trust forwarded this "
            "request, so the caller's address is not believed"
        )
    if found.channel.id != "tailnet":
        return ""
    return why_unread(TAILNET_KIND) or (
        f"not read: {label}, because no device in the tailnet reading holds {found.address}"
    )


# ----- Joining the request to the walked path ----------------------------------


def joined(walked: ServicePath | None, request, found: Connection | None = None) -> ServicePath:
    """``walked`` from the caller's device, each hop joined to ``request``."""

    from .connection import connection

    found = found if found is not None else connection(request)
    host = split_host_port(request.get_host())[0]
    primary = walked.primary if walked is not None else None
    hops = primary.hops if primary is not None else (_hq_only(host),)
    context = _Context(request, found, host, hops)
    route = Route(
        primary.via if primary is not None else "HQ",
        tuple(_joined_hop(hop, context) for hop in (caller_hop(found), *hops)),
        port=primary.port if primary is not None else None,
    )
    route = _with_layers(route, found.layers)
    if walked is None:
        return ServicePath(normalized_hostname(host) if is_hostname(host) else host, (route,))
    return ServicePath(walked.hostname, (route, *walked.routes[1:]), walked.unread, walked.observed)


def _hq_only(host: str) -> Hop:
    from .hq_self import site_label

    return Hop("hq", "HQ", site_label() or host)


@dataclass(frozen=True)
class _Context:
    request: Any
    found: Connection
    host: str
    hops: tuple[Hop, ...]

    @property
    def answered_on(self) -> str:
        """The machine holding the address HQ answered this request on, or one
        its host routes from; "" when none HQ knows does."""

        from .hq_self import own_addresses, served_at
        from .locate import index_of

        index = index_of(declared=self.found.declared)
        return next(
            (name for address in own_addresses(served_at(self.request)) if (name := index.at(address))),
            "",
        )

    @property
    def last_machine(self) -> str:
        return next((hop.name for hop in reversed(self.hops) if hop.step == "machine"), "")

    def header(self, name: str) -> str:
        return str(self.request.META.get(f"HTTP_{name.upper().replace('-', '_')}", "") or "").strip()


def _joined_hop(hop: Hop, context: _Context) -> Hop:
    judge = _JUDGES.get(hop.step)
    if judge is None:
        return hop
    evidence, check = judge(hop, context)
    return replace(_with_leg(hop, context.found), evidence=evidence, check=check)


def _with_leg(hop: Hop, found: Connection) -> Hop:
    """The tailnet hop as the caller's presence reads it: direct or relayed,
    and when the two keys last shook hands."""

    presence = found.presence
    if hop.step != "network" or presence is None or network_of(hop.detail) != "tailnet":
        return hop
    return replace(
        hop,
        name=found.leg_label,
        detail=f"{hop.detail} · handshake {presence.handshake}",
        source=Source(TAILNET_KIND, presence.connection_ref, presence.observed_at),
    )


def _with_layers(route: Route, layers) -> Route:
    """Each layer on the first hop of its step, or on HQ's hop."""

    steps = [hop.step for hop in route.hops]
    placed: dict[int, list] = {}
    for layer in layers:
        step = LAYER_STEPS.get(layer.id, "hq")
        index = steps.index(step) if step in steps else len(steps) - 1
        placed.setdefault(index, []).append(layer)
    return replace(
        route,
        hops=tuple(
            replace(hop, layers=tuple(placed.get(index, ()))) if index in placed else hop
            for index, hop in enumerate(route.hops)
        ),
    )


# ----- Each hop against the request --------------------------------------------


def _device(hop: Hop, context: _Context):
    found = context.found
    chain = address_chain(context.request)
    evidence = tuple(item for item in chain if item.role != "proxy")
    return evidence, _device_check(found)


def _device_check(found: Connection) -> Check:
    channel = found.channel.id
    if found.untrusted_forwarding:
        return Check(
            CONTRADICTED,
            f"{found.address} forwarded this request but is not in HQ's proxy "
            "allowlist, so the caller it names is not believed.",
            "If that address is your proxy, add it to SEVERINO_TRUSTED_PROXIES; "
            "otherwise find what is forwarding to HQ.",
        )
    if channel == "opaque":
        return Check(
            CONTRADICTED,
            "Every address in the forwarded chain is a trusted proxy, so the "
            "request names no caller and HQ is judging a proxy.",
            "Have the proxy pass the client's address in X-Forwarded-For.",
        )
    if channel == "tailnet":
        return _tailnet_device_check(found)
    if channel in {"network", "loopback"}:
        if found.machine_name:
            return Check(
                PROVEN,
                f"HQ knows {found.machine_name} at {found.address}, the address this "
                "request came from.",
            )
        return Check(
            UNPROVEN,
            f"{found.address} is on the local network, and no reading names the "
            "device holding it.",
        )
    return Check(
        CONTRADICTED,
        f"{found.address} is in none of the ranges HQ recognises.",
        "Enforce the trusted-network gate (SEVERINO_ENFORCE_TRUSTED_NETWORK).",
    )


def _tailnet_device_check(found: Connection) -> Check:
    device = found.caller_device
    if device is not None:
        return Check(
            PROVEN,
            f"The tailnet reading places {device.label} at {found.address}, the "
            "address this request came from.",
        )
    gap = why_unread(TAILNET_KIND)
    if gap:
        return Check(UNPROVEN, gap[:1].upper() + gap[1:] + ".")
    return Check(
        CONTRADICTED,
        f"This request came from {found.address}, a tailnet address no device in "
        "the tailnet reading holds.",
        "Refresh the tailnet reading. If the address stays unknown, it belongs to "
        "a node the tailnet connection cannot see, such as one shared in from "
        "another tailnet.",
    )


def _dns(hop: Hop, context: _Context):
    evidence = (Evidence("Host", context.host, "The name this request asked for."),)
    if not is_hostname(context.host):
        return evidence, Check(
            UNPROVEN, "This request named an address, not a name, so no DNS answer was involved."
        )
    return evidence, Check(
        PROVEN,
        f"This request asked for {normalized_hostname(context.host)}, the name "
        f"this {hop.label.lower()} answers with {hop.name or 'an address'}.",
    )


def _network(hop: Hop, context: _Context):
    found = context.found
    # A caller off every private range arrived from the internet.
    arrived = {"elsewhere": "public"}.get(found.channel.id, found.channel.id)
    expected = network_of(hop.detail)
    here = NETWORK_LABELS.get(expected, expected).lower()
    evidence = (Evidence("Arrived from", found.address, found.channel.label),)
    if arrived not in NETWORK_LABELS:
        return evidence, Check(
            UNPROVEN, "The caller's address did not reach HQ, so the request cannot say which network it used."
        )
    if arrived == expected:
        return evidence, Check(
            PROVEN,
            f"The name answers with an address on the {here}, and this request "
            f"arrived from {found.address}, on the {here} too.",
        )
    came = NETWORK_LABELS[arrived].lower()
    return evidence, Check(
        CONTRADICTED,
        f"The name answers with {hop.detail} on the {here}, but this request "
        f"arrived from {found.address} on the {came}.",
        f"Check that the device resolves {context.host} through the resolver the "
        "readings describe, and that it is connected the way that answer expects.",
    )


def _machine(hop: Hop, context: _Context):
    own = context.answered_on
    if hop.unread:
        return (), Check(UNPROVEN, hop.unread[:1].upper() + hop.unread[1:] + ".")
    from .hq_self import served_at, served_port

    served = ", ".join(served_at(context.request))
    port = served_port(context.request)
    evidence = (
        (Evidence("Answered at", f"{served}:{port}" if port else served, "The address this request reached HQ on."),)
        if served
        else ()
    )
    if own and hop.name == own:
        return evidence, Check(PROVEN, f"HQ answered this request on {own}.")
    if own and hop.name == context.last_machine:
        return evidence, Check(
            CONTRADICTED,
            f"The readings put HQ behind {hop.name}, but HQ answered this request on {own}.",
            f"Correct the proxy host's forward address, or the declaration that places {hop.name}.",
        )
    if own:
        return evidence, Check(
            UNPROVEN, "A request cannot show which machines it passed before the one HQ runs on."
        )
    return evidence, Check(
        UNPROVEN,
        "HQ answered on an address no machine it knows holds, so the request "
        "cannot say which machine this is.",
    )


def _ingress(hop: Hop, context: _Context):
    request = context.request
    peer = socket_peer(request)
    forwarded = bool(forwarded_chain(request))
    chain = tuple(item for item in address_chain(request) if item.role == "proxy")
    evidence = chain + _forwarding_headers(hop, context)
    if hop.unread:
        return evidence, Check(UNPROVEN, hop.unread[:1].upper() + hop.unread[1:] + ".")
    proxy = _proxy_name(hop)
    if not forwarded:
        return evidence, Check(
            CONTRADICTED,
            f"The readings say {proxy} fronts {context.host}, but this request "
            f"reached HQ directly from {peer}, without passing it.",
            "Bind HQ to an address only the proxy can reach, so no request skips it.",
        )
    if not is_trusted_proxy(peer):
        return evidence, Check(
            CONTRADICTED,
            f"{peer} forwarded this request but is not in HQ's proxy allowlist.",
            f"If {peer} is {proxy}, add it to SEVERINO_TRUSTED_PROXIES.",
        )
    return evidence, _forwarder_check(hop, context, peer, proxy)


def _forwarder_check(hop: Hop, context: _Context, peer: str, proxy: str) -> Check:
    headers = _declared_headers(hop)
    if not headers:
        return Check(PROVEN, f"A trusted proxy at {peer} forwarded this request.")
    agrees = next(
        (layer for layer in context.found.layers if layer.id == "proxy-evidence"), None
    )
    if agrees is None or not agrees.conclusive:
        return Check(
            UNPROVEN,
            f"A trusted proxy at {peer} forwarded this request without {proxy}'s "
            f"own {' and '.join(headers)} headers, so it cannot show it was {proxy}.",
        )
    if not agrees.holds:
        return Check(
            CONTRADICTED,
            f"{proxy}'s own headers disagree with the address HQ judged.",
            "Check the proxy host's forwarding configuration.",
        )
    return Check(
        PROVEN,
        f"{peer} forwarded this request with {proxy}'s own headers, and they agree "
        "with the address HQ judged.",
    )


def _proxy_name(hop: Hop) -> str:
    kind = hop.source.kind if hop.source is not None else ""
    spec = PROVIDERS.get(kind)
    if spec is None:
        return hop.label
    return next(
        (CONNECTION_LABELS[name] for name in spec.connection_providers if name in CONNECTION_LABELS),
        spec.label or hop.label,
    )


def _declared_headers(hop: Hop) -> tuple[str, ...]:
    kind = hop.source.kind if hop.source is not None else ""
    spec = PROVIDERS.get(kind)
    return tuple(spec.forwarding_headers) if spec is not None else ()


def _forwarding_headers(hop: Hop, context: _Context) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(name, value, f"Set by {_proxy_name(hop)}.")
        for name in _declared_headers(hop)
        if (value := context.header(name))
    )


def _edge(hop: Hop, context: _Context):
    headers = _access_headers(context.host)
    if not headers:
        return (), Check(
            UNPROVEN, "The edge adds no header HQ reads, so a request cannot show it passed the edge."
        )
    present = [name for name in headers if context.header(name)]
    evidence = tuple(
        Evidence(name, f"present, {len(context.header(name))} characters", "Never shown.")
        for name in present
    )
    if not present:
        return evidence, Check(
            CONTRADICTED,
            f"Access guards {context.host}, but this request carries no Access "
            "assertion: it did not pass through Access.",
            "Check that the name is proxied through the edge and that HQ cannot be "
            "reached around it.",
        )
    return evidence, Check(
        UNPROVEN,
        "The Access assertion arrived. HQ does not verify its signature, so it "
        "shows the header was set, not who signed it.",
    )


def _access_headers(host: str) -> tuple[str, ...]:
    from .facts import Subject, readings

    if not is_hostname(host):
        return ()
    about = readings().about(Subject.of(hostnames=(normalized_hostname(host),)))
    return tuple(dict.fromkeys(joined.spec.request_header for joined in about if joined.spec.request_header))


def _container(hop: Hop, context: _Context):
    return (), Check(UNPROVEN, "A request cannot show which container answered it.")


def _hq(hop: Hop, context: _Context):
    evidence = (
        Evidence("Host", context.host, "Checked against the hosts HQ will answer for."),
        Evidence(
            "Scheme",
            "https" if context.request.is_secure() else "http",
            "Whether the request arrived encrypted, as HQ decided it.",
        ),
    )
    return evidence, Check(PROVEN, f"This request reached HQ at {context.host}.")


_JUDGES = {
    "device": _device,
    "dns": _dns,
    "network": _network,
    "machine": _machine,
    "ingress": _ingress,
    "edge": _edge,
    "container": _container,
    "hq": _hq,
}


# ----- The projection -----------------------------------------------------------


@dataclass(frozen=True)
class RequestPath:
    """Everything the connection page, the API and MCP say about one request."""

    connection: Connection
    path: ServicePath
    addresses: tuple[Address, ...]
    hq_addresses: tuple[Address, ...]
    headers: tuple[Header, ...]

    @property
    def route(self) -> Route:
        return self.path.routes[0]

    @property
    def hops(self) -> tuple[Hop, ...]:
        """The hops the page draws: forwarding addresses are detail."""

        return tuple(hop for hop in self.route.hops if hop.step != "upstream")

    @property
    def chain(self) -> tuple[Evidence, ...]:
        """Every address in the forwarded chain, in the order it happened."""

        return tuple(item for hop in self.hops for item in hop.evidence if item.role)

    @property
    def findings(self) -> tuple[Hop, ...]:
        return tuple(hop for hop in self.hops if hop.check is not None and hop.check.state == CONTRADICTED)

    @property
    def proof(self) -> str:
        checked = [hop for hop in self.hops if hop.check is not None]
        proven = sum(hop.check.state == PROVEN for hop in checked)
        text = f"{proven} of {len(checked)} hops proven by this request"
        return f"{text} · {len(self.findings)} contradicted" if self.findings else text


def request_path(request) -> RequestPath:
    """How ``request`` reached HQ, read once per projection."""

    from .connection import addresses_of, addresses_of_hq, connection, headers_of
    from .connection_security import observed_request_controls
    from .paths import hq_path

    edge, firewall = observed_request_controls(request.get_host())
    found = connection(request, edge=edge, firewall=firewall)
    path = hq_path(request, found=found) or joined(None, request, found)
    return RequestPath(
        connection=found,
        path=path,
        addresses=addresses_of(found),
        hq_addresses=addresses_of_hq(found),
        headers=headers_of(request),
    )



def serialize_request_path(found: RequestPath) -> dict[str, Any]:
    """The request path as every adapter returns it: its hops as the items."""

    from .derived_reads import serialize_hop, serialize_layer
    from .projection import iso

    hops = [serialize_hop(hop) for hop in found.hops]
    connection = found.connection
    return {
        "items": hops,
        "count": len(hops),
        "hostname": found.path.hostname,
        "proof": found.proof,
        "findings": [
            {"step": hop.step, "label": hop.label, "detail": hop.check.detail, "fix": hop.check.step}
            for hop in found.findings
        ],
        "unread": list(found.path.gaps),
        "connection": {
            "address": connection.address,
            "channel": connection.channel.id,
            "channel_label": connection.channel.label,
            "summary": connection.summary,
            "transport": connection.transport_path,
            "forwarded": connection.forwarded,
            "untrusted_forwarding": connection.untrusted_forwarding,
            "device": connection.peer_label,
            "path": connection.path_label,
            "carried_over": connection.peering.label,
            "handshake": connection.handshake,
            "measured_by": connection.measurement_label,
            "tailnet_observed_at": iso(connection.tailnet_observed_at),
            "identity_agrees": connection.identity.corroborated,
            "layers": [serialize_layer(layer) for layer in connection.layers],
        },
        "addresses": [_address_row(row) for row in found.addresses],
        "hq_addresses": [_address_row(row) for row in found.hq_addresses],
        "headers": [
            {"name": header.name, "value": header.value, "state": header.state}
            for header in found.headers
        ],
    }


def _address_row(row: Address) -> dict[str, Any]:
    return {"value": row.value, "kind": row.kind, "label": row.label, "source": row.source}
