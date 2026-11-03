"""The pages that explain how a request was forwarded, given a request as asgi.py's middleware hands it on.

The forwarded-header middleware replaces the socket's peer with the caller it
resolves, so these pages read the peer from where the middleware records it;
read from the caller instead, a trusted proxy's forwarding reads as a
stranger's. Each test sends its request through the application's own
middleware list (hq/config/asgi.py), as production does.
"""

from asgiref.sync import async_to_sync
from django.core.handlers.asgi import ASGIRequest
from django.test import TestCase, override_settings

from .. import arrivals
from ..request_connection import connection
from ..request_path import address_chain
from .people import someone

PROXY = "127.0.0.1"
CALLER = "100.64.0.5"


def through_the_proxy():
    """A request the proxy on loopback forwarded for CALLER, as Django is handed it."""

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
        transport = httpx2.ASGITransport(app=composed, client=(PROXY, 50000))
        async with httpx2.AsyncClient(transport=transport, base_url="https://hq.example.test") as client:
            await client.get("/connection/", headers={"X-Forwarded-For": CALLER, "X-Forwarded-Proto": "https"})

    async_to_sync(run)()
    request = ASGIRequest(seen, None)
    request.user = someone()
    return request


@override_settings(SEVERINO_TRUSTED_PROXIES=["127.0.0.0/8"], ALLOWED_HOSTS=["hq.example.test"])
class ForwardedByATrustedProxyTests(TestCase):
    def test_the_connection_page_names_the_proxy_as_a_trusted_forwarder(self):
        found = connection(through_the_proxy())

        self.assertEqual(found.address, CALLER)
        self.assertTrue(found.forwarded)
        self.assertFalse(found.untrusted_forwarding)
        forwarder = next(layer for layer in found.layers if layer.id == "forwarder")
        self.assertTrue(forwarder.holds)
        self.assertEqual(forwarder.evidence, PROXY)

    def test_the_path_page_shows_the_proxy_hop_and_judges_the_caller(self):
        chain = address_chain(through_the_proxy())

        self.assertEqual([(hop.value, hop.role) for hop in chain], [(CALLER, "judged"), (PROXY, "proxy")])

    def test_an_arrival_records_the_proxy_that_forwarded_it(self):
        self.addCleanup(arrivals.forget)

        arrival = arrivals._arrival(through_the_proxy(), CALLER)

        self.assertEqual(arrival["address"], CALLER)
        self.assertEqual(arrival["forwarded_by"], PROXY)
