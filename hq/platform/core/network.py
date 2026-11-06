"""Where a request came from, and whether that place may reach HQ at all.

HQ is meant to be reachable from a private network and a VPN, never from the
internet, and whether a deployment is so lives in its DNS, firewall and router,
which the application cannot see. So it states the rule itself: private ranges,
the VPN's and the healthcheck's loopback are served, and anywhere else refused.
"""

import logging
from functools import cache
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

from django.conf import settings
from django.http import HttpResponseForbidden
lazy from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .write_bounds import NOTHING, writes

logger = logging.getLogger("severino.network")


@cache
def _networks(cidrs: tuple[str, ...]) -> tuple:
    parsed = []
    for cidr in cidrs:
        try:
            parsed.append(ip_network(cidr.strip(), strict=False))
        except ValueError:
            # A typo in configuration must not widen the gate. Dropping the
            # entry makes the rule stricter than intended, which is the safe
            # direction to be wrong in, and saying so names the typo.
            logger.warning(
                "A configured network %r is no network, so it is left out.",
                cidr,
                extra={"event": "network.entry.unreadable"},
            )
            continue
    return tuple(parsed)


def split_host_port(value: str) -> tuple[str, str]:
    """An endpoint as its address and its port, either of which may be empty.

    Every reader of a value a proxy or daemon wrote (`10.0.0.4:53812`,
    `[::1]:8000`) splits it here: a split slightly different elsewhere is how a
    hop stops matching a trusted proxy while still looking correct.
    """

    candidate = str(value or "").strip()
    if not candidate:
        return "", ""
    if candidate.startswith("["):
        host, _, rest = candidate.partition("]")
        return host.lstrip("["), rest.lstrip(":")
    if candidate.count(":") == 1:
        # host:port for IPv4. A bare IPv6 address has more than one colon, so
        # this cannot truncate one.
        host, _, port = candidate.rpartition(":")
        return host, port
    return candidate, ""


def strict_host(value: str) -> str:
    """The host a Host header names, lowercased with no trailing dot; "" if malformed.

    For boundaries that fail closed: an unclosed bracket, text after one, or a
    port that is not a number rejects the whole value rather than guessing.
    """

    candidate = str(value or "").strip().lower()
    if candidate.startswith("["):
        closing = candidate.find("]")
        suffix = candidate[closing + 1 :] if closing != -1 else ""
        if closing == -1 or (suffix and not (suffix[:1] == ":" and suffix[1:].isdigit())):
            return ""
    host, port = split_host_port(candidate)
    if port and not port.isdigit():
        return ""
    if candidate.count(":") == 1 and not port:
        return ""
    return host.rstrip(".")


def is_address(text: str) -> bool:
    """Whether ``text`` is exactly an IP address: no port, no name."""

    try:
        ip_address(text)
    except ValueError:
        return False
    return True


def parse_ip(value: str):
    """An address object, or None if this is not one.

    Unparseable input returns None and is treated as untrusted everywhere it is
    used: a hop that fails to parse must never be mistaken for a trusted one.
    """

    host, _ = split_host_port(value)
    if not host:
        return None
    try:
        return ip_address(host)
    except ValueError:
        return None


def _within(value, cidrs: tuple[str, ...]) -> bool:
    address = value if isinstance(value, IPv4Address | IPv6Address) else parse_ip(value)
    if address is None:
        return False
    return any(address in network for network in _networks(tuple(cidrs)))


def is_trusted_proxy(value) -> bool:
    return _within(value, tuple(settings.SEVERINO_TRUSTED_PROXIES))


def trusted_proxies() -> list[str]:
    """SEVERINO_TRUSTED_PROXIES as networks, in the form uvicorn's middleware reads.

    Parsed here, so an entry that is no network is dropped as everywhere else
    rather than kept by uvicorn as a literal: its `*` trusts every peer, and a
    name would be matched against the text of a forwarded hop.
    """

    return [str(network) for network in _networks(tuple(settings.SEVERINO_TRUSTED_PROXIES))]


# Where the address that opened the socket is kept, namespaced as the ASGI
# specification asks of a key it does not define. In a WSGI environ, as the
# test client builds one, the same key is in META; no request header can name
# it, since a header's key always starts with HTTP_.
SOCKET_PEER = "hq.socket_peer"


class ForwardedCaller:
    """The caller's address and scheme, from a proxy's forwarded headers, for every route.

    Uvicorn's ProxyHeadersMiddleware decides: it reads X-Forwarded-For and
    X-Forwarded-Proto only from a peer in SEVERINO_TRUSTED_PROXIES, and
    resolves the chain to the first hop from the right that is not one. It
    replaces the scope's client, so the peer is recorded first, for the pages
    that show how a request was forwarded. The server runs with
    --no-proxy-headers, or its own copy would resolve the caller before the
    peer is recorded.
    """

    def __init__(self, app):
        self.app = ProxyHeadersMiddleware(app, trusted_hosts=trusted_proxies())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            scope[SOCKET_PEER] = (scope.get("client") or ("",))[0]
        await self.app(scope, receive, send)


def client_ip(request) -> str:
    """The caller's address, as ForwardedCaller resolved it: the one HQ judges and records."""

    return str(request.META.get("REMOTE_ADDR", "") or "").strip()


def socket_peer(request) -> str:
    """The address that opened the socket to HQ, which a trusted proxy's forwarding replaced."""

    recorded = getattr(request, "scope", request.META).get(SOCKET_PEER)
    return str(recorded or request.META.get("REMOTE_ADDR", "") or "").strip()


def is_trusted_address(address: str) -> bool:
    return _within(address, tuple(settings.SEVERINO_TRUSTED_NETWORKS))


def is_trusted_client(request) -> bool:
    return is_trusted_address(client_ip(request))


class TrustedNetworkASGI:
    """The same rule, for what is mounted beside Django rather than inside it.

    Starlette serves static assets above the Django stack, where the middleware
    never sees them, so a boundary has no exception to reason about; the
    predicate is shared, so the two cannot disagree about who is trusted.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and settings.SEVERINO_ENFORCE_TRUSTED_NETWORK
            and not is_trusted_address((scope.get("client") or ("",))[0])
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"Forbidden."})
            return
        await self.app(scope, receive, send)


@writes(NOTHING)
class TrustedNetworkMiddleware:
    """Refuse anything arriving from outside the LAN, the tailnet or loopback.

    First in the chain, so an address that may not talk to HQ reaches no login
    form, spends no query and leaves no audit row. A refusal names no range it
    would have accepted, which would hand a scanner the map.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.SEVERINO_ENFORCE_TRUSTED_NETWORK and not is_trusted_client(request):
            return HttpResponseForbidden("Forbidden.")
        return self.get_response(request)
