"""/mcp/ answers as the MCP authorization spec says: a 401 or 403 a client can
act on, metadata naming where to sign in, and the gates in front of both."""

import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings

from hq.platform.api.tests.tokens import ISSUER, RESOURCE as API_RESOURCE, _serving, _token
from hq.platform.application.security import AuthorizationError

from . import test_mcp
from .test_mcp import RESOURCE

METADATA = "https://hq.example.test/.well-known/oauth-protected-resource/api"


def _challenge(headers):
    """The WWW-Authenticate parameters, by name."""

    import re

    scheme, _, rest = headers["www-authenticate"].partition(" ")
    return scheme, {name: value.replace('\\"', '"') for name, value in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', rest)}


class ChallengeTests(test_mcp.MCPBoundaryTests):
    def test_an_unauthenticated_request_is_told_where_to_sign_in(self):
        for token in (None, "wrong"):
            with self.subTest(token=token):
                headers = {}

                status, body = self._request(token=token, headers_seen=headers)

                self.assertEqual(status, 401)
                scheme, challenge = _challenge(headers)
                self.assertEqual(scheme, "Bearer")
                self.assertEqual(challenge["error"], "invalid_token")
                self.assertEqual(challenge["resource_metadata"], METADATA)
                self.assertEqual(body["error"], "invalid_token")

    def test_the_401_names_no_set_smaller_than_the_metadata_lists(self):
        """A client asks first for the scope a 401 names, so a narrower one would cap every sign-in."""

        from ..security import token_scopes

        for token in (None, "wrong"):
            with self.subTest(token=token):
                headers = {}
                self._request(token=token, headers_seen=headers)

                named = _challenge(headers)[1].get("scope")
                if named is not None:
                    self.assertEqual(set(named.split()), set(token_scopes()))

    def test_a_token_granting_nothing_usable_is_told_the_scope_it_needs(self):
        def ungranted(bearer):
            raise AuthorizationError('This token grants no HQ permissions. "Add" them.')

        headers = {}
        status, body = self._request(verifier=ungranted, headers_seen=headers)

        self.assertEqual(status, 403)
        _, challenge = _challenge(headers)
        self.assertEqual(challenge["error"], "insufficient_scope")
        self.assertEqual(challenge["scope"], "read")
        self.assertEqual(challenge["resource_metadata"], METADATA)
        self.assertEqual(body["error"], "insufficient_scope")


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=API_RESOURCE, SEVERINO_API_LEEWAY_SECONDS=30, OIDC_RP_SIGN_ALGO="RS256"
)
class RealAdmissionTests(test_mcp.MCPBoundaryTests):
    """Through HQ's own admission, as the ASGI module wires it."""

    def _through_hq(self, token, **request):
        from hq.config import asgi

        with _serving():
            return self._request(token=token, admit=asgi.admit_agent, **request)

    def test_a_valid_bearer_is_served_as_it_is_today(self):
        """Claude Code's headers helper sends a bearer it minted itself; nothing about it changes."""

        status, _ = self._through_hq(_token(scope="read"))

        self.assertEqual(status, 204)

    def test_a_verified_token_with_no_hq_permission_is_a_403_insufficient_scope(self):
        headers = {}

        status, _ = self._through_hq(_token(scope=""), headers_seen=headers)

        self.assertEqual(status, 403)
        self.assertEqual(_challenge(headers)[1]["scope"], "read")

    def test_an_expired_or_foreign_token_is_a_401(self):
        for token in (_token(aud="https://elsewhere.example.test"), _token(iss="https://evil.example.test")):
            with self.subTest(token=token):
                self.assertEqual(self._through_hq(token)[0], 401)


class GateTests(test_mcp.MCPBoundaryTests):
    """HQ's own Host and Origin gate, kept because the SDK's would not refuse all it refuses."""

    def test_hosts_are_matched_by_name_whatever_their_port_or_case(self):
        for host in ("a-docker-host", "a-docker-host:8000", "A-Docker-Host.", "a-docker-host:443"):
            with self.subTest(host=host):
                self.assertEqual(self._request(host=host)[0], 204)

    def test_a_host_that_is_not_one_or_a_port_that_is_not_a_number_is_refused(self):
        for host in ("hq.example.com", "a-docker-host:abc", "a-docker-host:8000/x", "a-docker-host.example", ""):
            with self.subTest(host=host):
                status, body = self._request(host=host)
                self.assertEqual((status, body["error"]), (400, "invalid_host"))

    def test_an_origin_is_refused_unless_allowlisted_and_none_is_admitted(self):
        allowed = ("https://agent.example.com",)
        cases = (
            (None, 204),
            ("https://agent.example.com", 204),
            ("https://agent.example.com:8443", 403),
            ("https://evil.example", 403),
            ("null", 403),
        )
        for origin, expected in cases:
            with self.subTest(origin=origin):
                self.assertEqual(self._request(origin=origin, allowed_origins=allowed)[0], expected)

    def test_the_sdks_host_match_admits_a_port_hq_refuses(self):
        """Why the gate is HQ's: the SDK's ``host:*`` takes any text after the colon.

        Should this ever fail, the SDK has closed the gap and its own settings
        can replace the gate.
        """

        from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

        sdk = TransportSecurityMiddleware(
            TransportSecuritySettings(allowed_hosts=["a-docker-host", "a-docker-host:*"], allowed_origins=[])
        )

        self.assertTrue(sdk._validate_host("a-docker-host:abc"))
        self.assertEqual(self._request(host="a-docker-host:abc")[0], 400)


class ProxiedPeerTests(test_mcp.MCPBoundaryTests):
    """Behind the TLS proxy, the caller is the address the proxy forwarded, and only from the proxy."""

    def _proxied(self, *, peer, forwarded):
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        from ..security import MCPBoundary, TailnetGate

        sent = []
        gate = TailnetGate(allowed_hosts=("hq.example.test",), allowed_networks=("100.64.0.0/10",))
        boundary = MCPBoundary(
            self._allowed_app, gate=gate, admit=test_mcp.admitting(test_mcp._accept_the_example_token)
        )
        application = Starlette(
            routes=[Mount("/api/mcp", app=boundary)],
            middleware=[Middleware(ProxyHeadersMiddleware, trusted_hosts=["127.0.0.0/8"])],
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/mcp/",
            "raw_path": b"/api/mcp/",
            "query_string": b"",
            "headers": [
                (b"host", b"hq.example.test"),
                (b"authorization", f"Bearer {test_mcp.TOKEN}".encode()),
                (b"x-forwarded-for", forwarded.encode()),
                (b"x-forwarded-proto", b"https"),
            ],
            "client": (peer, 40000),
            "server": ("127.0.0.1", 8000),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        async_to_sync(application)(scope, receive, send)
        return next(message["status"] for message in sent if "status" in message)

    def test_a_tailnet_caller_forwarded_by_the_proxy_is_served(self):
        self.assertEqual(self._proxied(peer="127.0.0.1", forwarded="100.64.0.10"), 204)

    def test_a_lan_caller_forwarded_by_the_proxy_is_not(self):
        self.assertEqual(self._proxied(peer="127.0.0.1", forwarded="192.0.2.10"), 404)

    def test_a_forwarded_address_from_anyone_else_is_ignored(self):
        self.assertEqual(self._proxied(peer="192.0.2.10", forwarded="100.64.0.10"), 404)


class ApplicationProxyTests(SimpleTestCase):
    # Whose forwarded headers the application reads: core's test_forwarded_caller.

    def test_mcp_is_served_under_the_machine_apis_resource(self):
        """One audience for both doors: a client's resource for /api/mcp/ is allowed under /api."""

        from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

        from hq.config.asgi import application, mcp_application

        (mount,) = [route for route in application.routes if getattr(route, "app", None) is mcp_application]
        served = resource_url_from_server_url(f"https://hq.example.test{mount.path}/")
        self.assertTrue(check_resource_allowed(served, "https://hq.example.test/api"))


class MetadataTests(TestCase):
    def _get(self, *, resource=RESOURCE, client="100.64.0.10", host="a-docker-host", origin=None, method="GET"):
        from ..security import ResourceMetadata, TailnetGate

        metadata = ResourceMetadata(
            gate=TailnetGate(allowed_hosts=("a-docker-host",), allowed_networks=("100.64.0.0/10",)),
            resource=resource,
            authorization_server=ISSUER,
        )
        headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/.well-known/oauth-protected-resource",
            "raw_path": b"/.well-known/oauth-protected-resource",
            "query_string": b"",
            "headers": headers,
            "client": (client, 40000),
            "server": ("127.0.0.1", 8000),
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        async_to_sync(metadata)(scope, receive, send)
        status = next(message["status"] for message in sent if "status" in message)
        body = b"".join(message.get("body", b"") for message in sent)
        return status, json.loads(body) if body else {}

    def test_it_names_the_identity_provider_and_the_audience_a_token_is_asked_for(self):
        status, body = self._get()

        self.assertEqual(status, 200)
        # Exactly as written, since a token's `aud` is compared with it as a string.
        self.assertEqual(body["resource"], RESOURCE)
        self.assertEqual(body["authorization_servers"], [ISSUER])
        self.assertEqual(body["bearer_methods_supported"], ["header"])

    def test_it_lists_what_an_agent_may_be_granted_and_nothing_only_a_person_holds(self):
        from hq.platform.application.security import HELD_ONLY_AT, Capability, operator_capabilities

        listed = set(self._get()[1]["scopes_supported"])

        self.assertIn("read", listed)
        self.assertFalse(listed & {name for name, where in HELD_ONLY_AT.items() if "mcp" not in where})
        self.assertLessEqual(
            listed, {item.value if isinstance(item, Capability) else item for item in operator_capabilities()}
        )

    def test_it_lists_every_permission_a_token_is_held_to(self):
        """A sign-in asks for what is listed, and the identity provider refuses one asking for less
        than a refusal tells a person to grant, so the list is the agent's whole grantable set."""

        from hq.platform.application.security import agent_grant, operator_capabilities

        listed = set(self._get()[1]["scopes_supported"])

        self.assertEqual(listed, set(agent_grant(str(name) for name in operator_capabilities())))

    def test_it_stands_behind_the_gates_of_mcp(self):
        for refused, expected in (
            ({"client": "192.0.2.10"}, 404),
            ({"host": "elsewhere"}, 400),
            ({"origin": "https://evil.example"}, 403),
        ):
            with self.subTest(refused=refused):
                self.assertEqual(self._get(**refused)[0], expected)

    def test_it_is_off_without_a_resource_and_read_only(self):
        self.assertEqual(self._get(resource="")[0], 404)
        self.assertEqual(self._get(method="POST")[0], 405)

    def test_it_is_where_the_sdk_looks_for_it(self):
        from mcp.server.auth.routes import build_resource_metadata_url
        from pydantic import AnyHttpUrl

        from ..security import metadata_url

        for resource in (RESOURCE, f"{RESOURCE}/", f"{RESOURCE}/mcp", f"{RESOURCE}:8443/api"):
            with self.subTest(resource=resource):
                self.assertEqual(metadata_url(resource), str(build_resource_metadata_url(AnyHttpUrl(resource))))
