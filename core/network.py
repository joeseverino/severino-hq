"""Where a request came from, and whether that place may reach HQ at all.

HQ holds provider credentials, a controller that can change public DNS, and an
audit trail of everything the business does. It is meant to be reachable from a
private network and a VPN, never from the internet.

Whether that is *true* of any given deployment is a property of its DNS, its
firewall and its router -- none of which this application can see. A new proxy
host, a forwarded port, or a container published to the wrong interface would
each change the answer silently, and HQ would never notice it had begun
answering strangers. So the application states the rule itself rather than
inheriting it: requests are served for private ranges, the VPN's range, and the
loopback address the container healthcheck probes -- and refused, before
authentication runs, for anywhere else.

The hard part is not the rule. It is knowing who is asking.

Behind a reverse proxy every request arrives from the proxy, whose address is
private and therefore always passes. A check written against `REMOTE_ADDR`
alone would be decorative: it would approve the proxy, every time, no matter
who was on the other side of it. The real client is in `X-Forwarded-For` --
which any client can also simply send, so believing it unconditionally is worse
than not checking at all, because it lets an attacker choose the address HQ
judges them by.

Both failures come from one question: which hops in that header did
infrastructure write, and which did a stranger? Only the peer that actually
connected is known to be truthful. So the chain is walked from the right --
from the hop the trusted proxy observed -- discarding proxies HQ has been told
about, and stopping at the first address it has not. That address is the
closest thing to the caller that HQ can prove, and anything further left is
attacker-controlled text.
"""

from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address, ip_network

from django.conf import settings
from django.http import HttpResponseForbidden


@lru_cache(maxsize=None)
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

    Proxies and daemons write ports (`10.0.0.4:53812`) and brackets
    (`[::1]:8000`) into the values HQ reads back, and every reader of those
    values needs the same two rules applied the same way. Held here because
    getting it slightly different somewhere else is how a hop stops matching a
    trusted proxy while still looking correct.
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
    address = parse_ip(value) if not hasattr(value, "version") else value
    if address is None:
        return False
    return any(address in network for network in _networks(tuple(cidrs)))


def is_trusted_proxy(value) -> bool:
    return _within(value, tuple(settings.SEVERINO_TRUSTED_PROXIES))


def client_ip(request) -> str:
    """The closest address to the caller that HQ can actually vouch for.

    Returns the peer address unless that peer is a proxy HQ has been told to
    believe, in which case the forwarded chain is walked from the right for the
    first hop that is not itself a known proxy.
    """

    peer = str(request.META.get("REMOTE_ADDR", "") or "").strip()
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded or not is_trusted_proxy(peer):
        # Not behind a proxy we know, so the header is unverifiable hearsay and
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

    Static assets are served by Starlette, above the Django stack, so the
    middleware below never sees them -- a request refused everywhere else still
    collected the stylesheet. Nothing secret is in there, but a boundary with a
    documented exception is one people reason about incorrectly later, and the
    fix is a wrapper rather than a second copy of the rule: the predicate is
    shared, so the two can never drift into disagreeing about who is trusted.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and settings.SEVERINO_ENFORCE_TRUSTED_NETWORK:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
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

    First in the chain, and deliberately so. This runs before sessions, before
    authentication and before any view: an address that may not talk to HQ
    should not be able to reach the login form, spend a database query, or
    appear in the audit log as an attempt at anything.

    A refusal says only that the address was refused. Naming the ranges that
    would have been accepted would hand a scanner the map.
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
