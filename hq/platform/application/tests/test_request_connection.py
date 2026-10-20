"""What HQ can prove about the request in front of it.

The value of this panel is entirely in whether a check can come back false. A
page that says "secure" no matter what is decoration, and decoration in a
security surface is worse than nothing: it is read as a measurement. So most of
what follows takes a fact away and asserts the page notices.
"""

from datetime import UTC, datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection as database_connection
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..moments import when
from ..request_addresses import addresses_of, addresses_of_hq
from ..request_channel import channel_for_request, channel_of
from ..request_connection import connection
from ..request_headers import headers_of
from ..request_path import address_chain
from ..ui import MISSING
from .host_only import host_only
from .markup import select, texts, within

A_TAILNET_ADDRESS = "100.64.0.5"
A_LAN_ADDRESS = "10.0.0.50"


def a_tailnet_policy(**lock):
    """A swept policy carrying one tailnet-lock reading."""

    ProviderInventory.objects.update_or_create(
        kind="tailscale.policy",
        defaults={
            "records": [{"lock": lock}],
            "observed_at": timezone.now(),
        },
    )


def a_tailnet(*devices):
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={"records": list(devices), "observed_at": timezone.now()},
    )


def a_device(name, address, *, user="", tags=(), observer=False, reach=(), **extra):
    """A swept device; a peer has a key and a completed handshake unless told otherwise."""

    return {
        "name": name,
        "addresses": [address],
        "user": user,
        "tags": list(tags),
        "self": observer,
        "reach": [
            {"port": port, "who": list(who), "rules": [{"who": list(who), "to": [f"{name}:{port}"], "line": 1}]}
            for port, who in reach
        ],
        "public_key": f"test-key-{name}",
        "last_handshake": timezone.now().isoformat(),
        **extra,
    }


def a_request(address=A_TAILNET_ADDRESS, *, host="hq.example.test", secure=True):
    request = RequestFactory().get("/connection/", secure=secure, HTTP_HOST=host, REMOTE_ADDR=address)
    request.user = get_user_model()(username="someone", email="someone@example.test")
    return request


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class ChannelTests(TestCase):
    """The one part that runs on every page, so it may not touch anything."""

    def test_a_tailscale_address_is_the_tailnet(self):
        self.assertEqual(channel_of(A_TAILNET_ADDRESS).id, "tailnet")

    def test_a_private_address_is_the_local_network(self):
        self.assertEqual(channel_of(A_LAN_ADDRESS).id, "network")

    def test_a_public_address_is_not_recognized(self):
        self.assertEqual(channel_of("203.0.113.7").id, "elsewhere")

    def test_nonsense_is_not_quietly_treated_as_private(self):
        """The safe direction to be wrong in."""

        self.assertFalse(channel_of("not-an-address").private)

    def test_deciding_the_channel_costs_no_queries(self):
        with self.assertNumQueries(0):
            channel_of(A_TAILNET_ADDRESS)

    @override_settings(SEVERINO_TRUSTED_PROXIES=["10.0.0.0/8"])
    def test_deciding_the_request_channel_also_costs_no_queries(self):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS)

        with self.assertNumQueries(0):
            self.assertEqual(channel_for_request(request).id, "tailnet")


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class LayerTests(TestCase):
    def setUp(self):
        self.own_addresses = mock.patch(
            "hq.platform.application.hq_self.host_addresses",
            return_value=frozenset({"100.64.0.9"}),
        )
        self.own_addresses.start()
        self.addCleanup(self.own_addresses.stop)
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device(
                "hq-host",
                "100.64.0.9",
                observer=True,
                reach=[(443, ["someone@example.test"])],
            ),
        )

    def layer(self, found, layer_id):
        return next(layer for layer in found.layers if layer.id == layer_id)

    def test_a_tailnet_request_satisfies_every_layer(self):
        # Every input this asserts on is pinned rather than inherited: the
        # point is that the layers agree when the conditions hold, not that
        # whichever settings the runner happened to load are the right ones.
        with override_settings(
            SEVERINO_ENFORCE_TRUSTED_NETWORK=True,
            AUTHENTICATION_BACKENDS=["hq.platform.core.oidc.HQOIDCAuthenticationBackend"],
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            SESSION_COOKIE_NAME="__Host-sessionid",
            SECURE_SSL_REDIRECT=True,
            SECURE_HSTS_SECONDS=31536000,
        ):
            found = connection(a_request())

        self.assertTrue(found.holds, [layer.id for layer in found.failing])
        self.assertTrue(all(layer.boundary for layer in found.layers))
        self.assertTrue(all(layer.mechanism for layer in found.layers))

    def test_a_request_off_the_tailnet_says_so(self):
        with override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True):
            found = connection(a_request(A_LAN_ADDRESS))

        self.assertFalse(self.layer(found, "channel").holds)

    def test_an_undeclared_forwarding_peer_is_not_labeled_as_the_caller(self):
        request = a_request(A_LAN_ADDRESS)
        request.META["HTTP_X_FORWARDED_FOR"] = A_TAILNET_ADDRESS

        with override_settings(SEVERINO_TRUSTED_PROXIES=[]):
            found = connection(request)

        self.assertTrue(found.untrusted_forwarding)
        self.assertTrue(found.forwarded)
        self.assertIsNone(found.device)
        self.assertEqual(found.reported_device.label, "a-laptop")
        self.assertEqual(found.caller_device.label, "a-laptop")
        self.assertEqual(found.peer_label, "a-laptop")
        self.assertEqual(found.peer_address, A_TAILNET_ADDRESS)
        self.assertEqual(found.address, A_LAN_ADDRESS)
        self.assertEqual(found.channel.id, "network")
        self.assertEqual(found.identity.tailnet_user, "")
        forwarder = self.layer(found, "forwarder")
        self.assertFalse(forwarder.holds)
        self.assertTrue(forwarder.conclusive)
        self.assertEqual(forwarder.evidence, A_LAN_ADDRESS)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[A_LAN_ADDRESS])
    def test_a_trusted_forwarder_remains_a_hop_not_the_caller(self):
        request = a_request(A_LAN_ADDRESS)
        request.META["HTTP_X_FORWARDED_FOR"] = A_TAILNET_ADDRESS

        found = connection(request)

        self.assertFalse(found.untrusted_forwarding)
        self.assertTrue(found.forwarded)
        self.assertEqual(found.peer_label, "a-laptop")
        self.assertEqual(found.address, A_TAILNET_ADDRESS)
        forwarder = self.layer(found, "forwarder")
        self.assertTrue(forwarder.holds)
        self.assertTrue(forwarder.conclusive)
        self.assertEqual(forwarder.evidence, A_LAN_ADDRESS)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[A_LAN_ADDRESS])
    def test_npm_redundant_headers_corroborate_the_canonical_inputs(self):
        request = a_request(A_LAN_ADDRESS)
        request.META.update(
            HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS,
            HTTP_X_REAL_IP=A_TAILNET_ADDRESS,
            HTTP_X_FORWARDED_SCHEME="https",
        )

        layer = self.layer(connection(request), "proxy-evidence")

        self.assertTrue(layer.holds)
        self.assertTrue(layer.conclusive)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[A_LAN_ADDRESS])
    def test_disagreeing_npm_headers_are_a_visible_proxy_failure(self):
        request = a_request(A_LAN_ADDRESS)
        request.META.update(
            HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS,
            HTTP_X_REAL_IP="100.64.0.77",
            HTTP_X_FORWARDED_SCHEME="https",
        )

        layer = self.layer(connection(request), "proxy-evidence")

        self.assertFalse(layer.holds)
        self.assertTrue(layer.conclusive)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[A_LAN_ADDRESS])
    def test_the_corroborating_headers_are_the_ones_a_proxy_kind_declares(self):
        from dataclasses import replace

        from hq.domains.control_plane.providers import PROVIDERS

        request = a_request(A_LAN_ADDRESS)
        request.META.update(
            HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS,
            HTTP_X_CLIENT=A_TAILNET_ADDRESS,
            HTTP_X_SCHEME="https",
        )
        declared = replace(PROVIDERS["npm.proxy_host"], forwarding_headers=("X-Client", "X-Scheme"))
        with mock.patch.dict(PROVIDERS, {"npm.proxy_host": declared}):
            layer = self.layer(connection(request), "proxy-evidence")

        self.assertTrue(layer.holds)
        self.assertIn("X-Client=", layer.evidence)

    def test_a_tls_request_off_the_tailnet_does_not_claim_wireguard(self):
        found = connection(a_request(A_LAN_ADDRESS, secure=True))

        layer = self.layer(found, "transport")
        self.assertTrue(layer.holds)
        self.assertEqual(found.transport, layer.evidence)
        self.assertIn("TLS", layer.evidence)
        self.assertNotIn("WireGuard", layer.evidence)

    def test_a_plain_tailnet_request_names_wireguard_as_its_transport(self):
        found = connection(a_request(secure=False))

        layer = self.layer(found, "transport")
        self.assertTrue(layer.holds)
        self.assertEqual(found.transport, layer.evidence)
        self.assertIn("WireGuard", layer.evidence)
        self.assertNotIn("TLS", layer.evidence)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[A_LAN_ADDRESS])
    def test_a_proxied_tailnet_request_names_each_encrypted_segment(self):
        request = a_request(A_LAN_ADDRESS)
        request.META["HTTP_X_FORWARDED_FOR"] = A_TAILNET_ADDRESS

        path = connection(request).transport_path

        # Each encrypted segment, in the order the request crossed them.
        self.assertLess(path.index("TLS"), path.index("WireGuard"))

    def test_a_plain_lan_request_claims_no_verified_encryption(self):
        found = connection(a_request(A_LAN_ADDRESS, secure=False))

        layer = self.layer(found, "transport")
        self.assertFalse(layer.holds)
        self.assertEqual(found.transport, layer.evidence)
        self.assertNotIn("TLS", layer.evidence)
        self.assertNotIn("WireGuard", layer.evidence)

    def test_the_gate_reports_when_it_is_not_being_enforced(self):
        """The check that would otherwise be a sentence rather than a fact."""

        with override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=False):
            found = connection(a_request())

        self.assertFalse(self.layer(found, "gate").holds)

    @override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"])
    def test_an_installed_password_backend_is_reported(self):
        found = connection(a_request())

        self.assertFalse(self.layer(found, "sign-in").holds)

    @override_settings(SESSION_COOKIE_SECURE=False)
    def test_a_session_cookie_missing_a_flag_is_reported(self):
        found = connection(a_request())

        layer = self.layer(found, "session")
        self.assertFalse(layer.holds)
        self.assertIn("Secure=off", layer.evidence)

    def test_a_published_name_is_not_treated_as_a_defense(self):
        ProviderInventory.objects.update_or_create(
            kind="cloudflare.dns_record",
            defaults={
                "records": [{"name": "hq.example.test", "content": "203.0.113.7"}],
                "observed_at": timezone.now(),
            },
        )

        found = connection(a_request())

        self.assertFalse(self.layer(found, "name").holds)

    def test_an_unpublished_name_holds(self):
        self.assertTrue(self.layer(connection(a_request()), "name").holds)

    def test_the_policy_layer_reports_a_device_the_policy_refuses(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, tags=["tag:guest"]),
            a_device("hq-host", "100.64.0.9", observer=True, reach=[(443, ["tag:admin"])]),
        )

        found = connection(a_request())

        self.assertFalse(self.layer(found, "policy").holds)
        self.assertTrue(self.layer(found, "policy").conclusive)

    def test_a_policy_question_the_sweep_did_not_ask_is_unverified_not_denied(self):
        found = connection(a_request(host="hq.example.test:8443"))

        layer = self.layer(found, "policy")
        self.assertFalse(layer.holds)
        self.assertFalse(layer.conclusive)
        self.assertEqual(layer.state, "unknown")
        self.assertNotEqual(found.verdict, "confirmed")

    def test_a_device_awaiting_approval_is_not_a_known_node(self):
        """Being listed is not being admitted. A device pending approval is in
        the inventory and the tailnet carries nothing for it."""

        a_tailnet(a_device("a-laptop", "100.64.0.77", authorized=False))

        found = connection(a_request("100.64.0.77"))

        layer = self.layer(found, "device")
        self.assertFalse(layer.holds)
        self.assertIn("approved", layer.detail)

    def test_a_device_lock_has_not_signed_is_not_a_known_node(self):
        """Under tailnet lock an unsigned node is filtered out by every peer
        while reporting itself as perfectly healthy."""

        a_tailnet(
            a_device(
                "a-laptop",
                "100.64.0.77",
                lock_error="node is not signed by a trusted key.",
            )
        )

        found = connection(a_request("100.64.0.77"))

        layer = self.layer(found, "device")
        self.assertFalse(layer.holds)
        self.assertIn("tailnet lock", layer.detail)

    def test_a_device_nothing_swept_is_not_reported_as_a_known_node(self):
        found = connection(a_request("100.64.0.77"))

        self.assertFalse(self.layer(found, "device").holds)
        self.assertTrue(self.layer(found, "device").conclusive)

    def test_with_no_tailnet_read_a_device_is_unconfirmed_not_missing(self):
        """A fresh install has read no tailnet: that is not a device missing from one."""

        ProviderInventory.objects.filter(kind="tailscale.device").delete()

        layer = self.layer(connection(a_request(A_TAILNET_ADDRESS)), "device")

        self.assertFalse(layer.conclusive)
        self.assertEqual(layer.state, "unknown")


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class IdentityTests(TestCase):
    def setUp(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

    def test_two_independent_sources_agreeing_is_reported_as_such(self):
        self.assertTrue(connection(a_request()).identity.corroborated)

    def test_a_session_with_no_device_behind_it_is_not_corroborated(self):
        self.assertFalse(connection(a_request(A_LAN_ADDRESS)).identity.corroborated)

    def test_two_sources_naming_different_people_is_a_visible_conflict(self):
        request = a_request()
        request.user.email = "different@example.test"

        identity = connection(request).identity

        self.assertFalse(identity.corroborated)
        self.assertTrue(identity.conflicted)
        layer = next(layer for layer in connection(request).layers if layer.id == "identity-agreement")
        self.assertFalse(layer.holds)

    def test_an_sso_signed_link_correlates_different_identity_namespaces(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="operator@passkey"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )
        request = a_request()
        request.user.email = "operator@example.test"
        request.session = {
            "_auth_user_backend": "hq.platform.core.oidc.HQOIDCAuthenticationBackend",
            "oidc_tailscale_principal": "operator@passkey",
        }

        identity = connection(request).identity

        self.assertTrue(identity.corroborated)
        self.assertEqual(identity.agreement_basis, "SSO-signed principal link")

    def test_a_password_session_cannot_use_an_oidc_principal_link(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="operator@passkey"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )
        request = a_request()
        request.user.email = "operator@example.test"
        request.session = {
            "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
            "oidc_tailscale_principal": "operator@passkey",
        }

        self.assertFalse(connection(request).identity.corroborated)

    def test_different_identity_namespaces_are_unknown_without_a_link(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="operator@passkey"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        identity = connection(a_request()).identity

        self.assertFalse(identity.corroborated)
        self.assertFalse(identity.conflicted)

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT="https://identity.example.test/authorize")
    def test_a_configured_sso_provider_is_not_attributed_to_a_password_session(self):
        request = a_request()
        request.session = {"_auth_user_backend": "django.contrib.auth.backends.ModelBackend"}

        identity = connection(request).identity

        self.assertEqual(identity.route, "password")
        self.assertEqual(identity.provider, "")

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT="https://identity.example.test/authorize")
    def test_the_provider_that_signed_an_oidc_session_is_named(self):
        request = a_request()
        request.session = {"_auth_user_backend": "hq.platform.core.oidc.HQOIDCAuthenticationBackend"}

        identity = connection(request).identity

        self.assertEqual(identity.route, "single sign-on")
        self.assertEqual(identity.provider, "identity.example.test")


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class LinkTests(TestCase):
    def test_a_relayed_peer_is_not_described_as_direct(self):
        a_tailnet(
            a_device(
                "a-laptop",
                A_TAILNET_ADDRESS,
                user="someone@example.test",
                relay="ord",
                direct_endpoint="",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        self.assertIn("Relayed", connection(a_request()).path_label)

    def test_a_direct_peer_is_reported_with_its_endpoint(self):
        a_tailnet(
            a_device(
                "a-laptop",
                A_TAILNET_ADDRESS,
                direct_endpoint="203.0.113.9:41641",
                relay="ord",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        self.assertEqual(connection(a_request()).path_label, "Direct")

    def test_a_handshake_that_never_happened_is_not_reported_as_an_age(self):
        """Tailscale writes the zero time for never, which ages to two millennia."""

        a_tailnet(
            a_device(
                "a-laptop",
                A_TAILNET_ADDRESS,
                last_handshake="0001-01-01T00:00:00Z",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        self.assertEqual(connection(a_request()).handshake, MISSING)

    def test_no_device_is_said_as_why(self):
        unread = connection(a_request()).peer_label

        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))
        absent = connection(a_request()).peer_label

        # Two reasons, told apart: nothing read, and nothing at this address.
        self.assertTrue(unread)
        self.assertTrue(absent)
        self.assertNotEqual(unread, absent)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class ConnectionPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="someone", email="someone@example.test", password="not-used-here"
        )
        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))

    def test_the_page_needs_a_session(self):
        self.assertEqual(self.client.get(reverse("connection")).status_code, 302)

    def test_every_header_the_request_carried_is_shown(self):
        """Read, declined or ignored: the panel drops none, and a declined one says why."""

        self.client.force_login(self.user)

        response = self.client.get(reverse("connection"), HTTP_X_REAL_IP="100.64.0.5", HTTP_X_EXAMPLE="example")

        shown = [element.text.strip() for element in within(response, "data-connection-protocol")]
        for header in response.context["request_path"].headers:
            with self.subTest(header=header.name):
                self.assertTrue(any(text.startswith(f"{header.name}:") for text in shown))
        self.assertTrue(any(text.startswith("One address from the proxy.") for text in shown))

    def test_the_panel_the_dialog_lifts_out_is_on_the_page(self):
        """The dialog finds the panel by this hook, so its absence is a break."""

        self.client.force_login(self.user)

        response = self.client.get(reverse("connection"))

        self.assertContains(response, "data-connection-panel")
        # Each check once, on the hop that decides it.
        self.assertContains(
            response,
            "data-connection-layer=",
            count=len(response.context["connection"].layers),
        )
        self.assertContains(response, f'href="{reverse("control_plane:connections")}"')
        self.assertContains(response, f'href="{reverse("control_plane:topology")}"')
        self.assertContains(response, "data-connection-protocol")
        self.assertContains(response, "data-connection-layers")
        self.assertContains(response, "data-response-headers")
        # Folded until opened, which is when the response is read back.
        (protocol,) = select(response, "data-connection-protocol")
        self.assertNotIn("open", protocol.attrs)

    def test_identity_dates_use_hqs_normal_date_format(self):
        self.client.force_login(self.user)
        self.user.last_login = datetime(2026, 8, 23, 21, 12, tzinfo=UTC)
        self.user.save(update_fields=("last_login",))

        response = self.client.get(reverse("connection"))

        self.assertIn(when(self.user.last_login), texts(response, "time"))

    def test_the_request_timeline_includes_the_observed_npm_edge(self):
        ProviderInventory.objects.create(
            kind="npm.proxy_host",
            observed_at=timezone.now(),
            records=[
                {
                    "domain_names": ["testserver"],
                    "access_list_id": 7,
                    "access_policy": {
                        "satisfy_any": False,
                        "pass_auth": False,
                        "authorization_count": 0,
                        "implicit_deny": True,
                        "clients": [
                            {"directive": "allow", "address": "100.64.0.0/10"},
                            {
                                "directive": "allow",
                                "address": "fd7a:115c:a1e0::/48",
                            },
                        ],
                    },
                }
            ],
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("connection"))

        edge = next(layer for layer in response.context["connection"].layers if layer.id == "edge")
        self.assertTrue(edge.holds)
        self.assertTrue(edge.evidence)
        self.assertContains(response, escape(edge.evidence))


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class AddressEvidenceTests(TestCase):
    def test_received_lan_and_public_endpoints_are_both_shown_and_labeled(self):
        a_tailnet(
            a_device(
                "a-laptop",
                A_TAILNET_ADDRESS,
                endpoints=["10.0.0.50:41641", "198.51.100.8:41641"],
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        rows = {row.value: row for row in addresses_of(connection(a_request()))}

        self.assertEqual(rows["10.0.0.50:41641"].label, "Local network")
        self.assertEqual(rows["198.51.100.8:41641"].label, "Public")

    def test_path_endpoints_are_timestamped_snapshots_not_current_claims(self):
        a_tailnet(
            a_device(
                "a-laptop",
                A_TAILNET_ADDRESS,
                direct_endpoint="192.0.2.50:41641",
                endpoints=["192.0.2.50:41641"],
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        found = connection(a_request())
        rows = addresses_of(found)

        self.assertIsNotNone(found.tailnet_observed_at)
        (endpoint,) = [row for row in rows if row.value == "192.0.2.50:41641"]
        self.assertTrue(endpoint.source)

    def test_hq_tunnel_endpoints_are_not_presented_as_service_addresses(self):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS),
            a_device(
                "hq-host",
                "100.64.0.9",
                observer=True,
                endpoints=["192.0.2.9:41641"],
            ),
        )

        with mock.patch(
            "hq.platform.application.hq_self.host_addresses",
            return_value=frozenset({"100.64.0.9"}),
        ):
            rows = addresses_of_hq(connection(a_request()))

        self.assertEqual([row.value for row in rows], ["100.64.0.9"])


@override_settings(
    ALLOWED_HOSTS=["hq.example.test", "testserver"],
    SEVERINO_TRUSTED_PROXIES=["10.0.0.0/8", "172.16.0.0/12"],
)
class HopTests(TestCase):
    """Which hop HQ believes, and the working shown for it.

    The quietly consequential decision on the page: believe the wrong hop and
    the network gate is judging a proxy rather than a caller, which it does
    silently and looks identical from every other surface.
    """

    def hops(self, *, peer, forwarded=""):
        request = RequestFactory().get(
            "/connection/",
            HTTP_HOST="hq.example.test",
            REMOTE_ADDR=peer,
            **({"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {}),
        )
        return address_chain(request)

    def test_the_caller_is_the_last_hop_a_known_proxy_observed(self):
        found = self.hops(peer="10.0.0.9", forwarded="100.64.0.5")

        judged = [hop for hop in found if hop.role == "judged"]
        self.assertEqual([hop.value for hop in judged], ["100.64.0.5"])

    def test_hops_further_left_are_not_believed(self):
        """Anything past the first unknown hop is text the caller chose."""

        found = self.hops(peer="10.0.0.9", forwarded="203.0.113.9, 100.64.0.5")

        self.assertEqual(found[0].role, "ignored")
        self.assertEqual(found[0].value, "203.0.113.9")

    def test_a_chain_of_only_proxies_says_nothing_identified_the_caller(self):
        """The shape a proxy that drops the client address produces."""

        found = self.hops(peer="10.0.0.9", forwarded="172.18.0.1")
        named = self.hops(peer="10.0.0.9", forwarded="100.64.0.5")

        judged = next(hop for hop in found if hop.role == "judged")
        # HQ falls back to the proxy's address and says that is what it did.
        self.assertEqual(judged.value, "10.0.0.9")
        self.assertNotEqual(judged.detail, next(hop for hop in named if hop.role == "judged").detail)

    def test_a_forwarded_header_from_an_unknown_peer_is_not_believed(self):
        """Otherwise a caller picks the address HQ judges them by."""

        found = self.hops(peer="203.0.113.9", forwarded="100.64.0.5")

        self.assertEqual([hop.value for hop in found], ["203.0.113.9"])

    def test_the_chain_reads_in_the_order_the_hops_happened(self):
        found = self.hops(peer="10.0.0.9", forwarded="100.64.0.5")

        self.assertEqual([hop.value for hop in found], ["100.64.0.5", "10.0.0.9"])


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class HeaderTests(TestCase):
    """The raw inputs, shown without ever showing a credential."""

    def headers(self, **extra):
        request = RequestFactory().get("/connection/", HTTP_HOST="hq.example.test", **extra)
        return {header.name: header for header in headers_of(request)}

    def test_a_header_hq_acts_on_says_what_it_is_for(self):
        found = self.headers(HTTP_X_FORWARDED_FOR="100.64.0.5")

        self.assertTrue(found["X-Forwarded-For"].used)

    def test_a_header_nothing_reads_is_listed_as_unread(self):
        """A proxy sending something nothing looks at is otherwise invisible."""

        found = self.headers(HTTP_X_REAL_IP="100.64.0.5")

        self.assertFalse(found["X-Real-Ip"].used)

    def test_the_session_cookie_is_never_printed(self):
        found = self.headers(HTTP_COOKIE="sessionid=super-secret-value")

        self.assertNotIn("super-secret-value", found["Cookie"].value)
        self.assertTrue(found["Cookie"].redacted)

    def test_an_authorization_header_is_never_printed(self):
        found = self.headers(HTTP_AUTHORIZATION="Bearer a-real-token")

        self.assertNotIn("a-real-token", found["Authorization"].value)

    def test_a_credential_a_gateway_adds_is_never_printed(self):
        found = self.headers(
            HTTP_CF_ACCESS_JWT_ASSERTION="gateway-jwt",
            HTTP_X_API_KEY="gateway-key",
            HTTP_X_AUTH_REQUEST_ACCESS_TOKEN="gateway-token",
        )

        for name in ("Cf-Access-Jwt-Assertion", "X-Api-Key", "X-Auth-Request-Access-Token"):
            with self.subTest(name=name):
                self.assertTrue(found[name].redacted)
                self.assertNotIn("gateway", found[name].value)

    def test_headers_hq_acts_on_come_first(self):
        found = headers_of(
            RequestFactory().get(
                "/connection/",
                HTTP_HOST="hq.example.test",
                HTTP_X_REAL_IP="100.64.0.5",
                HTTP_X_FORWARDED_FOR="100.64.0.5",
            )
        )

        self.assertTrue(found[0].used)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class CostTests(TestCase):
    """What this costs the pages that merely carry the control.

    The badge is in the header of every page in HQ, so anything it costs is
    paid on every render. The panel behind it is the expensive part (it reads
    two inventories and evaluates the access policy) and it is fetched when
    somebody opens it and not before.
    """

    def setUp(self):
        # Counts what the host draws; an installed extension adds its own reads.
        self.enterContext(host_only())
        self.user = get_user_model().objects.create_user(
            username="someone", email="someone@example.test", password="not-used-here"
        )
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

    def test_the_badge_costs_nothing(self):
        """Arithmetic on one address: no query, no settings read, no inventory."""

        from hq.platform.core.context_processors import connection as badge

        request = a_request()
        with self.assertNumQueries(0):
            badge(request)

    def cost(self) -> int:
        with CaptureQueriesContext(database_connection) as queries:
            self.client.get(reverse("connection"))
        return len(queries)

    def test_the_panel_costs_the_same_however_many_devices_there_are(self):
        """Not once per device, per address, per layer or per header."""

        self.client.force_login(self.user)
        few = self.cost()
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device("hq-host", "100.64.0.9", observer=True),
            *(a_device(f"another-{index}", f"100.64.1.{index}") for index in range(12)),
        )

        self.assertLessEqual(self.cost(), few)


@override_settings(
    ALLOWED_HOSTS=["hq.example.test", "testserver"],
    SEVERINO_TRUSTED_PROXIES=["10.0.0.0/8", "172.16.0.0/12"],
    SEVERINO_ENFORCE_TRUSTED_NETWORK=True,
)
class ProxyThatDropsTheCallerTests(TestCase):
    """A proxy chain that never names the caller is its own finding.

    Reporting the last proxy's network as though it were the caller's is the
    one confusion this page exists to prevent: it reads as a fact about a
    person and is a fact about a router. Worse, the network gate is then
    admitting the proxy, so anything able to reach the proxy passes it.
    """

    def setUp(self):
        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))

    def found(self):
        request = RequestFactory().get(
            "/connection/",
            secure=True,
            HTTP_HOST="hq.example.test",
            REMOTE_ADDR="10.0.0.9",
            HTTP_X_FORWARDED_FOR="172.18.0.1",
        )
        request.user = get_user_model()(username="someone")
        return connection(request)

    def test_it_says_the_address_never_arrived(self):
        self.assertEqual(self.found().channel.id, "opaque")

    def test_the_gate_does_not_claim_to_hold_while_judging_a_proxy(self):
        gate = next(layer for layer in self.found().layers if layer.id == "gate")

        self.assertFalse(gate.holds)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class DeclinedHeaderTests(TestCase):
    """A header carrying the right answer that HQ refuses to believe.

    The dangerous reading of "ignored" is that nobody wired it up, which
    invites somebody to wire it up. Declining `X-Real-IP` is the safer of two
    choices and the page has to be able to say which it is.
    """

    def header(self, name, **extra):
        request = RequestFactory().get("/connection/", HTTP_HOST="hq.example.test", **extra)
        return next(h for h in headers_of(request) if h.name == name)

    def test_a_single_asserted_address_is_declined_rather_than_unread(self):
        found = self.header("X-Real-Ip", HTTP_X_REAL_IP="100.64.0.5")

        self.assertEqual(found.state, "declined")
        self.assertIn("X-Forwarded-For", found.declined)

    def test_a_second_source_for_the_scheme_is_declined(self):
        found = self.header("X-Forwarded-Scheme", HTTP_X_FORWARDED_SCHEME="https")

        self.assertEqual(found.state, "declined")

    def test_a_header_nothing_has_an_opinion_on_is_merely_ignored(self):
        found = self.header("Accept-Language", HTTP_ACCEPT_LANGUAGE="en")

        self.assertEqual(found.state, "ignored")

    def test_declined_headers_sort_above_ignored_ones(self):
        found = headers_of(
            RequestFactory().get(
                "/connection/",
                HTTP_HOST="hq.example.test",
                HTTP_ACCEPT_LANGUAGE="en",
                HTTP_X_REAL_IP="100.64.0.5",
            )
        )
        states = [h.state for h in found]

        self.assertLess(states.index("declined"), states.index("ignored"))


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class ServingDeviceTests(TestCase):
    """Which node HQ says it is running on.

    The sweep's ``self`` flag marks the device whose daemon took the reading,
    the controller's host. That is the same machine as HQ's only when the two
    share one, and HQ is not obliged to run there.
    """

    def setUp(self):
        from hq.domains.control_plane.models import ManagedResource

        # The controller swept from one node; HQ runs on the other.
        a_tailnet(
            a_device("example-controller", "100.64.0.9", observer=True),
            a_device(
                "example-registered-name",
                "100.64.0.5",
                dns_name="example-host.example-tailnet.ts.net",
            ),
        )
        # Only the declaration holds both halves: the address the host answers
        # at, and the address the tailnet knows it by.
        ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={
                "name": "example-host",
                "addresses": ["192.0.2.10", "100.64.0.5"],
                "role": "example",
            },
        )

    def serving(self, own, served=()):
        return self.resolution(own, served).device

    def resolution(self, own, served=()):
        from .. import tailnet
        from ..infrastructure import declared_machines
        from ..request_connection import _serving_device_resolution

        with mock.patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset(own)):
            return _serving_device_resolution(tailnet.devices(), declared_machines(), tuple(served))

    def test_locating_hq_never_puts_dns_in_the_request_path(self):
        from unittest import mock

        from ..hq_self import host_addresses

        host_addresses.cache_clear()
        try:
            with (
                mock.patch(
                    "hq.platform.application.hq_self.socket.getaddrinfo",
                    side_effect=AssertionError("DNS must not be consulted"),
                ),
                mock.patch("hq.platform.application.hq_self.socket.socket", side_effect=OSError),
            ):
                self.assertEqual(host_addresses(), frozenset({"127.0.0.1", "::1"}))
        finally:
            host_addresses.cache_clear()

    def test_the_node_hq_runs_on_is_found_by_its_own_address(self):
        found = self.serving({"100.64.0.5"})

        self.assertIsNotNone(found)
        self.assertEqual(found.name, "example-registered-name")

    def test_a_lan_only_host_still_resolves_through_its_declaration(self):
        """The bridge case: the tailnet address is on an interface the host's
        own hostname does not resolve to, so only the declaration joins them."""

        found = self.serving({"192.0.2.10"})

        self.assertIsNotNone(found)
        self.assertEqual(found.name, "example-registered-name")
        self.assertTrue(self.resolution({"192.0.2.10"}).verified)

    def test_the_address_a_request_arrived_on_places_hq(self):
        resolved = self.resolution(set(), served=("192.0.2.10",))

        self.assertTrue(resolved.verified)
        self.assertEqual(resolved.device.name, "example-registered-name")

    def test_a_declared_address_with_a_port_still_places_hq(self):
        ManagedResource.objects.filter(key="example-host").update(
            spec={"name": "example-host", "addresses": ["192.0.2.10:22", "100.64.0.5"]}
        )

        self.assertTrue(self.resolution({"192.0.2.10"}).verified)

    def test_loopback_never_places_hq(self):
        self.assertFalse(self.resolution({"127.0.0.1", "::1"}).verified)

    def test_the_observer_flag_is_not_mistaken_for_where_hq_runs(self):
        found = self.serving({"192.0.2.10"})

        self.assertNotEqual(found.name, "example-controller")

    def test_a_host_that_cannot_place_itself_falls_back_to_the_observer(self):
        found = self.serving(set())

        self.assertIsNotNone(found)
        self.assertEqual(found.name, "example-controller")
        self.assertFalse(self.resolution(set()).verified)

    def test_an_observer_fallback_is_not_drawn_or_authorized_as_hq(self):
        with mock.patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset()):
            found = connection(a_request())

        self.assertIsNone(found.serves)
        self.assertEqual(found.observer.name, "example-controller")
        policy = next(layer for layer in found.layers if layer.id == "policy")
        self.assertFalse(policy.conclusive)

    def test_an_unrecognized_address_never_guesses_a_node(self):
        found = self.serving({"198.51.100.7"})

        self.assertEqual(found.name, "example-controller")

    def test_the_label_is_what_names_a_node_to_a_person(self):
        """Both sides of the panel must resolve a name the same way."""

        found = self.serving({"100.64.0.5"})

        self.assertEqual(found.label, "example-host")
        self.assertNotEqual(found.label, found.name)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class CarriageTests(TestCase):
    """Which network the tailnet session is actually riding over.

    Being on the tailnet says the traffic is encrypted and the peer enrolled.
    It says nothing about where the packets went, so without this a laptop in
    the next room and one in an airport lounge produce an identical page.
    """

    # A globally routable endpoint, built rather than written: the architecture
    # suite reads every tracked file for addresses and allows two literals.
    PUBLIC = "1.1.1.1"

    def _connection(self, **endpoint):
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test", **endpoint),
            a_device("hq-host", "100.64.0.9", observer=True),
        )
        return connection(a_request())

    def test_a_private_endpoint_means_nothing_crossed_the_internet(self):
        found = self._connection(direct_endpoint="10.9.9.9:41641")

        self.assertEqual(found.peering.id, "local")
        self.assertFalse(found.peering.public)

    def test_a_public_endpoint_names_the_address_to_ask_about(self):
        found = self._connection(direct_endpoint=f"{self.PUBLIC}:41641")

        self.assertEqual(found.peering.id, "internet")
        self.assertTrue(found.peering.public)
        self.assertEqual(found.peering.address, self.PUBLIC)

    def test_a_global_address_on_this_hosts_own_prefix_is_its_own_network(self):
        """Global by type, but the same /64 the host sits on: nothing crossed
        the internet, and saying it did is wrong."""

        from ipaddress import ip_network
        from unittest import mock

        with mock.patch(
            "hq.platform.application.request_connection.on_link_networks",
            return_value=(ip_network("2001:db8:0:1::/64"),),
        ):
            home = self._connection(direct_endpoint="[2001:db8:0:1::99]:41641").peering
            away = self._connection(direct_endpoint="[2001:db8:ffff::5]:41641").peering

        self.assertEqual(home.id, "local")
        self.assertFalse(home.public)
        self.assertEqual(away.id, "internet")

    def test_a_relayed_session_says_it_crosses_a_machine_neither_end_owns(self):
        found = self._connection(relay="dfw")

        self.assertEqual(found.peering.id, "relay")
        self.assertFalse(found.peering.public)

    def test_no_answer_is_three_different_sentences(self):
        """Conflating them blamed the network for a limit on HQ's own view."""

        a_tailnet()
        found = connection(a_request())
        self.assertEqual(found.peering.id, "unobserved")

        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))
        found = connection(a_request())
        self.assertEqual(found.peering.id, "unknown")

    def test_the_address_panel_is_offered_only_where_there_is_one_to_ask_about(self):
        """The wiring, asserted at the template rather than by hand.

        The disclosure never renders in development (the peering there is
        never `internet`) so nothing but this would notice it being dropped.
        """

        from django.template.loader import render_to_string

        public = self._connection(direct_endpoint=f"{self.PUBLIC}:41641")
        markup = render_to_string("core/_connection_panel.html", {"connection": public})
        self.assertIn("data-peering-detail", markup)
        self.assertIn(f"address={self.PUBLIC}", markup)

        local = self._connection(direct_endpoint="10.9.9.9:41641")
        markup = render_to_string("core/_connection_panel.html", {"connection": local})
        self.assertNotIn("data-peering-detail", markup)


@override_settings(
    ALLOWED_HOSTS=["hq.example.test", "testserver"],
    SEVERINO_TRUSTED_PROXIES=["127.0.0.1/32"],
)
class LocalProxyPolicyTests(TestCase):
    """A reverse proxy on HQ's own host still crosses one policed hop.

    The production shape: the proxy runs beside HQ and forwards from a loopback
    address. Asked as "device -> forwarder" the question has no target, because
    the tailnet has never heard of 127.0.0.1, so both policy layers returned
    nothing and the Zero trust policy boundary vanished from the page entirely,
    on the deployment where it matters most.

    The hop the policy governs is the one the caller dialled: their device to
    the node HQ runs on. One tailnet hop, so one layer: claiming a second for
    proxy-to-HQ would describe a loopback socket as a policed crossing.
    """

    def setUp(self):
        self.own_addresses = mock.patch(
            "hq.platform.application.hq_self.host_addresses",
            return_value=frozenset({"100.64.0.9"}),
        )
        self.own_addresses.start()
        self.addCleanup(self.own_addresses.stop)
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device(
                "hq-host",
                "100.64.0.9",
                observer=True,
                reach=[(443, ["someone@example.test"])],
            ),
        )

    def found(self):
        request = RequestFactory().get(
            "/connection/",
            secure=True,
            HTTP_HOST="hq.example.test",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS,
        )
        request.user = get_user_model()(username="someone", email="someone@example.test")
        return connection(request)

    def policy_layers(self):
        return [layer for layer in self.found().layers if layer.boundary == "Zero trust policy"]

    def test_the_boundary_does_not_disappear_behind_a_local_proxy(self):
        self.assertTrue(self.policy_layers())

    def test_it_asks_about_the_hop_the_caller_actually_dialled(self):
        layer = self.policy_layers()[0]

        self.assertEqual(layer.id, "policy")
        self.assertTrue(layer.holds)
        self.assertTrue(layer.conclusive)

    def test_it_does_not_invent_a_second_hop_for_a_loopback_socket(self):
        self.assertEqual(len(self.policy_layers()), 1)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class TailnetLockTests(TestCase):
    """The layer under the device layer: signed, or taken on trust.

    Everything above this one believes the coordination server handed out the
    right public key for this node. Only lock removes that belief, so the tests
    that matter are the ones where it is absent, off, or unsigned: a line that
    can only ever read "signed" is decoration.
    """

    def setUp(self):
        self.own_addresses = mock.patch(
            "hq.platform.application.hq_self.host_addresses",
            return_value=frozenset({"100.64.0.9"}),
        )
        self.own_addresses.start()
        self.addCleanup(self.own_addresses.stop)
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

    def layer(self, found):
        return next((item for item in found.layers if item.id == "tailnet-lock"), None)

    def test_an_unswept_tailnet_says_nothing_rather_than_off(self):
        # No reading is not a reading of "off". Rendering one would be HQ
        # reporting a setting it has never looked at.
        self.assertIsNone(self.layer(connection(a_request())))

    def test_lock_off_is_shown_and_never_counts_as_proof(self):
        a_tailnet_policy(enabled=False, trusted_keys=0)
        found = self.layer(connection(a_request()))
        self.assertFalse(found.holds)
        self.assertFalse(found.conclusive)

    def test_an_unsigned_node_fails_the_layer(self):
        a_tailnet_policy(enabled=True, trusted_keys=2)
        a_tailnet(
            a_device(
                "a-laptop",
                A_TAILNET_ADDRESS,
                user="someone@example.test",
                lock_error="No signature chains to a trusted key.",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )
        found = self.layer(connection(a_request()))
        self.assertFalse(found.holds)
        self.assertTrue(found.conclusive)
        self.assertIn("No signature chains", found.detail)

    def test_a_signed_node_under_lock_holds_and_counts_its_signers(self):
        a_tailnet_policy(enabled=True, trusted_keys=2)
        found = self.layer(connection(a_request()))
        self.assertTrue(found.holds)
        self.assertTrue(found.conclusive)
        self.assertIn("2 signing keys", found.evidence)

    def test_one_signing_key_is_not_pluralized(self):
        a_tailnet_policy(enabled=True, trusted_keys=1)
        self.assertTrue(self.layer(connection(a_request())).evidence.endswith("1 signing key"))

    def test_no_device_means_no_signature_line(self):
        # The device layer above has already said no node answers here. A line
        # about that node's signature would be describing nothing.
        a_tailnet_policy(enabled=True, trusted_keys=2)
        self.assertIsNone(self.layer(connection(a_request(A_LAN_ADDRESS))))


class DashboardConnectionTileTests(SimpleTestCase):
    """Only a failed check takes the attention tone; one HQ cannot confirm yet does not."""

    def tile(self, failing):
        from types import SimpleNamespace

        from django.template.loader import render_to_string

        connection = SimpleNamespace(
            channel=SimpleNamespace(id="tailnet", label="Tailnet"),
            transport="WireGuard + TLS",
            holds=False,
            failing=failing,
            summary="1 failed" if failing else "6 not confirmed",
            verdict="failing" if failing else "unconfirmed",
        )
        return render_to_string("core/_dashboard_connection.html", {"connection": connection})

    def test_an_unconfirmed_check_is_said_quietly(self):
        self.assertNotIn("is-attention", self.tile(failing=()))

    def test_a_failed_check_takes_the_attention_tone(self):
        self.assertIn("is-attention", self.tile(failing=("a layer",)))

    def test_the_dot_says_the_verdict_not_only_the_network(self):
        self.assertIn("conn-verdict-unconfirmed", self.tile(failing=()))


class VerdictTests(SimpleTestCase):
    """Green only when every check on the way in is confirmed."""

    def verdict(self, *layers):
        from types import SimpleNamespace

        from ..request_connection import Connection

        return Connection(
            address="100.64.0.20",
            channel=SimpleNamespace(id="tailnet"),
            device=None,
            serves=None,
            observer=None,
            identity=None,
            layers=tuple(SimpleNamespace(holds=held, conclusive=sure) for held, sure in layers),
        ).verdict

    def test_every_check_held_on_evidence_is_confirmed(self):
        self.assertEqual(self.verdict((True, True), (True, True)), "confirmed")

    def test_one_unconfirmed_check_is_not_green(self):
        self.assertEqual(self.verdict((True, True), (True, False)), "unconfirmed")

    def test_one_failed_check_is_failing(self):
        self.assertEqual(self.verdict((False, True), (True, False)), "failing")
