"""Where a request came from, and whether that place may reach HQ at all.

HQ is meant to be reachable from a private network and a VPN, never from the
internet, and whether a deployment is so lives in its DNS, firewall and router,
which the application cannot see. So it states the rule itself: private ranges,
the VPN's and the healthcheck's loopback are served, and anywhere else refused.
"""

from functools import cache
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

from django.conf import settings
from django.http import HttpResponseForbidden


@cache
def _networks(cidrs: tuple[str, ...]) -> tuple:
    parsed = []
    for cidr in cidrs:
        try:
            parsed.append(ip_network(cidr.strip(), strict=False))
        except ValueError:
            # A typo in configuration must not widen the gate. Dropping the
            # entry makes the rule stricter than intended, which is the safe
            # direction to be wrong in.
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


def client_ip(request) -> str:
    """The closest address to the caller that HQ can actually vouch for.

    Behind a proxy every request comes from the proxy's private address, and
    X-Forwarded-For is text any client can send, so the chain is walked from the
    right, past the proxies HQ was told to believe, to the first hop that is not
    one: the closest hop infrastructure wrote. Further left is the caller's text.
    """

    peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded or not is_trusted_proxy(peer):
        # Not behind a known proxy, so the header is unverifiable hearsay and
        # the peer is the only fact available.
        return peer
    hops = [hop.strip() for hop in str(forwarded).split(",") if hop.strip()]
    for hop in reversed(hops):
        if not is_trusted_proxy(hop):
            return hop
    # Every hop was a known proxy. The peer is as close to the caller as this
    # request gets.
    return peer


def is_trusted_client(request) -> bool:
    return _within(client_ip(request), tuple(settings.SEVERINO_TRUSTED_NETWORKS))


class TrustedNetworkASGI:
    """The same rule, for what is mounted beside Django rather than inside it.

    Starlette serves static assets above the Django stack, where the middleware
    never sees them, so a boundary has no exception to reason about; the
    predicate is shared, so the two cannot disagree about who is trusted.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and settings.SEVERINO_ENFORCE_TRUSTED_NETWORK:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])
            }
            peer = (scope.get("client") or ("", 0))[0]
            request = _FakeScopeRequest(peer, headers.get("x-forwarded-for", ""))
            if not is_trusted_client(request):
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


class _FakeScopeRequest:
    """Adapts an ASGI scope to the two keys `client_ip` reads."""

    def __init__(self, peer: str, forwarded: str):
        self.META = {"REMOTE_ADDR": peer}
        if forwarded:
            self.META["HTTP_X_FORWARDED_FOR"] = forwarded


class TrustedNetworkMiddleware:
    """Refuse anything arriving from outside the LAN, the tailnet or loopback.

    First in the chain, so an address that may not talk to HQ reaches no login
    form, spends no query and leaves no audit row. A refusal names no range it
    would have accepted, which would hand a scanner the map.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Django's SECURE_PROXY_SSL_HEADER is intentionally simple: when it is
        # enabled it believes X-Forwarded-Proto without deciding who sent it.
        # Make the proxy trust decision first so an arbitrary peer cannot turn
        # a plain request into an apparently secure one by writing a header.
        peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
        if request.META.get("HTTP_X_FORWARDED_PROTO") and not is_trusted_proxy(peer):
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
        if settings.SEVERINO_ENFORCE_TRUSTED_NETWORK and not is_trusted_client(request):
            return HttpResponseForbidden("Forbidden.")
        return self.get_response(request)
