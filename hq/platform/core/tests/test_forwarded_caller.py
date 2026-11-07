"""Who sent a request, and over which scheme, as the forwarded-header middleware decides it.

Every request passes `ForwardedCaller` (hq/config/asgi.py) before any route,
so these run requests through the application's own middleware and routes and
read what each boundary made of the caller it resolved.
"""

import re
from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.handlers.asgi import ASGIRequest
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings, tag

from hq.platform.core.models import AuditLog
from hq.platform.core.network import SOCKET_PEER, client_ip, socket_peer

from .forwarding import arrived

PROXY = "10.0.0.5"
TAILNET_CALLER = "100.64.0.1"
STRANGER = "203.0.113.9"


def _composition():
    """HQ's routes behind HQ's middleware, built under the settings in force now."""

    from starlette.applications import Starlette

    from hq.config.asgi import application

    return Starlette(routes=application.routes, middleware=application.user_middleware)


def _fetch(peer, path, *, forwarded=None, proto=None, headers=None, application=None, steps=None, browser=False):
    """A request from ``peer`` to the composed application, or ``steps(client)`` with a client of it.

    ``browser`` addresses HQ over https, as a browser behind the proxy does, so
    the Secure cookies HQ sets as the image runs it (the CSRF cookie among
    them) come back on the next request.
    """

    import httpx2

    sent = dict(headers or {})
    if forwarded is not None:
        sent["X-Forwarded-For"] = forwarded
    if proto is not None:
        sent["X-Forwarded-Proto"] = proto

    async def run():
        transport = httpx2.ASGITransport(app=application or _composition(), client=(peer, 50000))
        base_url = "https://testserver" if browser else "http://testserver"
        async with httpx2.AsyncClient(transport=transport, base_url=base_url) as client:
            if steps is not None:
                return await steps(client)
            return await client.get(path, headers=sent)

    return async_to_sync(run)()


def _seen(peer, **sent):
    """The request Django is handed for a request from ``peer``, through asgi.py's own middleware list."""

    from starlette.applications import Starlette
    from starlette.routing import Mount

    from hq.config.asgi import application

    seen = {}

    async def capture(scope, receive, send):
        seen.update(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    composed = Starlette(routes=[Mount("/", app=capture)], middleware=application.user_middleware)
    _fetch(peer, "/", headers=sent, application=composed)
    return ASGIRequest(seen, None)


@override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY])
class ClientAddressTests(SimpleTestCase):
    """Who HQ believes the caller is."""

    def caller(self, peer, forwarded=None):
        meta = {"REMOTE_ADDR": peer} | ({"HTTP_X_FORWARDED_FOR": forwarded} if forwarded is not None else {})
        return client_ip(RequestFactory().get("/", **arrived(**meta)))

    @override_settings(SEVERINO_TRUSTED_PROXIES=["127.0.0.0/8", "::1/128"])
    def test_the_shipped_default_does_not_believe_the_lan(self):
        """The default trusts only loopback, never a whole range.

        HQ binds the host network namespace, so a LAN or tailnet peer can reach
        the port without passing the proxy. Trusting the range let any of them
        name the address written into the audit log as the source of a failed
        sign-in: the same rows the throttle reads back.
        """

        self.assertEqual(self.caller("10.9.9.9", forwarded=STRANGER), "10.9.9.9")

    def test_peer_is_used_when_there_is_no_proxy(self):
        self.assertEqual(self.caller("100.101.102.103"), "100.101.102.103")

    def test_forwarded_header_from_a_stranger_is_ignored(self):
        """The whole point. Otherwise the gate is opened by asking politely."""

        self.assertEqual(self.caller(STRANGER, forwarded=TAILNET_CALLER), STRANGER)

    def test_forwarded_header_from_the_proxy_is_believed(self):
        self.assertEqual(self.caller(PROXY, forwarded=TAILNET_CALLER), TAILNET_CALLER)

    def test_the_chain_is_read_from_the_right(self):
        """Everything left of the last untrusted hop is attacker-written text."""

        self.assertEqual(self.caller(PROXY, forwarded=f"{TAILNET_CALLER}, {STRANGER}"), STRANGER)

    def test_a_spoofed_prefix_cannot_hide_the_real_caller(self):
        self.assertEqual(self.caller(PROXY, forwarded=f"10.0.0.9, {STRANGER}"), STRANGER)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY, "10.0.0.6"])
    def test_a_chain_of_trusted_proxies_resolves_to_the_first_hop_none_vouches_for(self):
        self.assertEqual(self.caller(PROXY, forwarded=f"{TAILNET_CALLER}, {STRANGER}, 10.0.0.6"), STRANGER)

    def test_a_hop_s_port_is_not_part_of_its_address(self):
        self.assertEqual(self.caller(PROXY, forwarded=f"{STRANGER}:41641"), STRANGER)

    @override_settings(SEVERINO_TRUSTED_PROXIES=["*"])
    def test_a_wildcard_trusts_no_one(self):
        """Uvicorn reads `*` as every peer; HQ's rule drops an entry that is no network."""

        self.assertEqual(self.caller(STRANGER, forwarded=TAILNET_CALLER), STRANGER)

    @override_settings(SEVERINO_TRUSTED_PROXIES=["10.0.0.1/24"])
    def test_a_network_written_with_host_bits_is_still_that_network(self):
        self.assertEqual(self.caller(PROXY, forwarded=TAILNET_CALLER), TAILNET_CALLER)


class ProxiedFixtureTests(SimpleTestCase):
    """A fixture written as the middleware's result is what the middleware makes of its proxy's request."""

    @override_settings(SEVERINO_TRUSTED_PROXIES=["172.18.0.0/16"])
    def test_each_proxied_fixture_is_its_proxys_request_resolved(self):
        from hq.platform.core.example_estate import REQUEST

        from .test_browser_fixtures import _THROUGH_THE_PROXY

        for name, fixture in {"browser connection page": _THROUGH_THE_PROXY, "example estate": REQUEST}.items():
            with self.subTest(fixture=name):
                sent = {key: value for key, value in fixture.items() if key not in {"secure", SOCKET_PEER}}
                handed = arrived(**(sent | {"REMOTE_ADDR": fixture[SOCKET_PEER]}))

                self.assertEqual(handed["REMOTE_ADDR"], fixture["REMOTE_ADDR"])
                self.assertEqual(handed[SOCKET_PEER], fixture[SOCKET_PEER])


@override_settings(SEVERINO_TRUSTED_PROXIES=["127.0.0.0/8"])
class ApplicationMiddlewareTests(SimpleTestCase):
    """What asgi.py's middleware list hands Django, in the order it applies them."""

    def test_the_peer_is_recorded_before_the_caller_replaces_it(self):
        request = _seen("127.0.0.1", **{"X-Forwarded-For": TAILNET_CALLER})

        self.assertEqual(client_ip(request), TAILNET_CALLER)
        self.assertEqual(socket_peer(request), "127.0.0.1")

    def test_a_client_header_cannot_name_the_peer(self):
        request = _seen(STRANGER, **{"hq.socket_peer": "127.0.0.1", "Hq-Socket-Peer": "127.0.0.1"})

        self.assertEqual(socket_peer(request), STRANGER)
        self.assertEqual(client_ip(request), STRANGER)

    def test_only_a_trusted_proxy_can_assert_https(self):
        self.assertTrue(_seen("127.0.0.1", **{"X-Forwarded-Proto": "https"}).is_secure())
        self.assertFalse(_seen("100.64.0.77", **{"X-Forwarded-Proto": "https"}).is_secure())


@override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True, SEVERINO_TRUSTED_PROXIES=[PROXY])
class TrustedNetworkGateTests(SimpleTestCase):
    """The gate judges the caller the middleware resolved, on pages and static files alike."""

    def status(self, path, peer, forwarded):
        return _fetch(peer, path, forwarded=forwarded).status_code

    def test_a_tailnet_caller_behind_the_proxy_is_served(self):
        self.assertEqual(self.status("/health/live/", PROXY, TAILNET_CALLER), 200)
        self.assertNotEqual(self.status(f"{settings.STATIC_URL}hq.css", PROXY, TAILNET_CALLER), 403)

    def test_a_public_client_behind_the_proxy_is_still_refused(self):
        """The realistic exposure: the proxy is trusted, the caller is not."""

        self.assertEqual(self.status("/health/live/", PROXY, STRANGER), 403)
        self.assertEqual(self.status(f"{settings.STATIC_URL}hq.css", PROXY, STRANGER), 403)

    def test_a_stranger_cannot_forge_a_tailnet_address(self):
        self.assertEqual(self.status("/health/live/", STRANGER, TAILNET_CALLER), 403)
        self.assertEqual(self.status(f"{settings.STATIC_URL}hq.css", STRANGER, TAILNET_CALLER), 403)


@override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY], SEVERINO_LOGIN_MAX_ATTEMPTS=3)
class SignInLockoutTests(TestCase):
    """The lockout counts the caller the middleware resolved, never the proxy or a forged hop."""

    def setUp(self):
        # As Django's own test clients do: a request served here would otherwise
        # close the connection that holds the test's transaction.
        from django.core.signals import request_finished, request_started
        from django.db import close_old_connections

        for signal in (request_started, request_finished):
            signal.disconnect(close_old_connections)
            self.addCleanup(signal.connect, close_old_connections)

    def attempts(self, peer, forwarded, usernames):
        """Failed sign-ins from ``peer``, each forwarding ``forwarded``; the status of each."""

        async def steps(client):
            page = await client.get("/accounts/login/", headers={"X-Forwarded-For": forwarded})
            token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)[1]
            answered = []
            for username in usernames:
                # A browser's form post names its origin, which Django's CSRF check requires over https.
                response = await client.post(
                    "/accounts/login/",
                    data={"username": username, "password": "wrong", "csrfmiddlewaretoken": token},
                    headers={"X-Forwarded-For": forwarded, "Origin": "https://testserver"},
                )
                answered.append(response.status_code)
            return answered

        return _fetch(peer, "", steps=steps, browser=True)

    def test_a_failure_is_recorded_from_the_forwarded_caller(self):
        self.attempts(PROXY, TAILNET_CALLER, ["someone"])

        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        self.assertEqual(entry.metadata["ip"], TAILNET_CALLER)

    def test_one_caller_behind_the_proxy_is_locked_out_and_another_is_not(self):
        sprayed = self.attempts(PROXY, TAILNET_CALLER, ["a", "b", "c", "d"])
        neighbor = self.attempts(PROXY, "100.64.0.2", ["e"])

        self.assertEqual(sprayed[-1], 429)
        self.assertNotEqual(neighbor[-1], 429)

    def test_a_forged_hop_neither_frames_another_caller_nor_escapes_its_own(self):
        forged = self.attempts("100.64.0.3", TAILNET_CALLER, ["a", "b", "c", "d"])
        framed = self.attempts(PROXY, TAILNET_CALLER, ["e"])

        self.assertEqual(forged[-1], 429)
        self.assertNotEqual(framed[-1], 429)
        self.assertEqual(
            set(AuditLog.objects.filter(action=AuditLog.Action.LOGIN_FAILED).values_list("metadata__ip", flat=True)),
            {"100.64.0.3", TAILNET_CALLER},
        )


@tag("source")
class ServerReadsNoForwardedHeadersTests(SimpleTestCase):
    """Uvicorn's own proxy-header reading would resolve the caller before the peer is recorded."""

    def test_every_launch_of_the_application_turns_it_off(self):
        root = Path(settings.BASE_DIR)
        launches = {
            "Dockerfile": (
                (root / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", ""),
                "hq.config.asgi:application",
            ),
            "tools/dev/dev.sh": (
                (root / "tools/dev/dev.sh").read_text(encoding="utf-8"),
                "hq.config.dev_asgi:migrated_application",
            ),
        }
        for name, (text, served) in launches.items():
            with self.subTest(launch=name):
                lines = [line for line in text.splitlines() if served in line]
                self.assertTrue(lines, f"{name} no longer launches the application")
                for line in lines:
                    self.assertIn("--no-proxy-headers", line)
