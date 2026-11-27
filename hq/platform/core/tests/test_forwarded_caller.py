"""Who sent a request, and over which scheme, as the forwarded-header middleware decides it.

Every request passes `ForwardedCaller` (hq/config/asgi.py) before any route,
so this runs requests through the application's own middleware list.
"""

from asgiref.sync import async_to_sync
from django.core.handlers.asgi import ASGIRequest
from django.test import SimpleTestCase, override_settings

from hq.platform.core.network import client_ip, socket_peer

PROXY = "10.0.0.5"
TAILNET_CALLER = "100.64.0.1"
STRANGER = "203.0.113.9"


def _seen(peer: str, headers: dict[str, str]) -> ASGIRequest:
    """The request Django is handed for one from ``peer``, through asgi.py's own middleware list."""

    import httpx2
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from hq.config.asgi import application

    seen = {}

    async def capture(scope, receive, send):
        seen.update(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def run():
        composed = Starlette(routes=[Mount("/", app=capture)], middleware=application.user_middleware)
        transport = httpx2.ASGITransport(app=composed, client=(peer, 50000))
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.get("/", headers=headers)

    async_to_sync(run)()
    return ASGIRequest(seen, None)


@override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY, "*"])
class ForwardedCallerTests(SimpleTestCase):
    def test_only_a_trusted_proxy_names_the_caller_or_its_scheme(self):
        # The peer, what it sent, and who HQ believes called over which scheme.
        # "*" in the setting is no network, so it trusts no one.
        cases = (
            (PROXY, {"X-Forwarded-For": TAILNET_CALLER, "X-Forwarded-Proto": "https"}, TAILNET_CALLER, True),
            (STRANGER, {"X-Forwarded-For": TAILNET_CALLER, "X-Forwarded-Proto": "https"}, STRANGER, False),
            # Everything left of the last hop no proxy vouches for is the caller's own text.
            (PROXY, {"X-Forwarded-For": f"{TAILNET_CALLER}, {STRANGER}:41641"}, STRANGER, False),
            # A client's header never names the socket's peer.
            (STRANGER, {"Hq-Socket-Peer": PROXY, "X-Forwarded-For": TAILNET_CALLER}, STRANGER, False),
        )
        for peer, headers, caller, secure in cases:
            with self.subTest(peer=peer, headers=headers):
                request = _seen(peer, headers)
                self.assertEqual((client_ip(request), socket_peer(request)), (caller, peer))
                self.assertIs(request.is_secure(), secure)
