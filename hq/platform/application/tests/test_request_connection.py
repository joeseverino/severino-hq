"""What HQ can prove about the request in front of it.

The value of this panel is entirely in whether a check can come back false. A
page that says "secure" no matter what is decoration, and decoration in a
security surface is worse than nothing: it is read as a measurement. So most of
what follows takes a fact away and asserts the page notices."""

from datetime import UTC, datetime
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.tests.forwarding import arrived

from ..moments import when
from ..request_channel import channel_for_request, channel_of
from ..request_connection import connection
from ..ui import MISSING
from .connection_requests import A_LAN_ADDRESS, A_TAILNET_ADDRESS, a_device, a_request, a_tailnet
from .markup import select, text, texts, within
from .people import someone


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
        request = RequestFactory().get("/", **arrived(REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS))

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
        request = a_request(A_LAN_ADDRESS, HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS)

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
        request = a_request(A_LAN_ADDRESS, HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS)

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
        request = a_request(
            A_LAN_ADDRESS,
            HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS,
            HTTP_X_REAL_IP=A_TAILNET_ADDRESS,
            HTTP_X_FORWARDED_SCHEME="https",
        )

        layer = self.layer(connection(request), "proxy-evidence")

        self.assertTrue(layer.holds)
        self.assertTrue(layer.conclusive)

    @override_settings(SEVERINO_TRUSTED_PROXIES=[A_LAN_ADDRESS])
    def test_disagreeing_npm_headers_are_a_visible_proxy_failure(self):
        request = a_request(
            A_LAN_ADDRESS,
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

        request = a_request(
            A_LAN_ADDRESS,
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
        request = a_request(A_LAN_ADDRESS, HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS)

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

    def test_no_device_leaves_the_name_out(self):
        """The address stands alone: no sentence fills the place of a name HQ
        does not have, whether it read the tailnet or not."""

        unread = connection(a_request())
        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))
        absent = connection(a_request())

        self.assertEqual((unread.peer_label, absent.peer_label), ("", ""))
        self.assertEqual(absent.peer_address, A_TAILNET_ADDRESS)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class ConnectionPageTests(TestCase):
    def setUp(self):
        self.user = someone()
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
        self.assertTrue(any(text.startswith("HQ reads X-Forwarded-For.") for text in shown))

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

    def test_each_response_check_is_a_row_the_browser_fills_with_both_answers_ready(self):
        """The server draws every row the browser reads back, each holding
        both states it can show, so the script only chooses one."""

        from ..request_headers import RESPONSE_CHECKS

        self.client.force_login(self.user)

        response = self.client.get(reverse("connection"))

        rows = {row.attrs["data-response-check"]: row for row in select(response, "data-response-check")}
        self.assertEqual(list(rows), [check.name for check in RESPONSE_CHECKS])
        states = {element.attrs["data-response-state"] for element in select(response, "data-response-state")}
        self.assertEqual(states, {"waiting", "present", "absent"})
        directives = {name for name, row in rows.items() if "data-response-directive" in row.attrs}
        self.assertEqual(directives, {check.name for check in RESPONSE_CHECKS if check.directive})

    def test_identity_dates_use_hqs_normal_date_format(self):
        self.client.force_login(self.user)
        self.user.last_login = datetime(2026, 8, 23, 21, 12, tzinfo=UTC)
        self.user.save(update_fields=("last_login",))

        response = self.client.get(reverse("connection"))

        # The page's words are read with whitespace collapsed, a no-break space with it.
        self.assertIn(" ".join(when(self.user.last_login).split()), texts(response, "time"))

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
        self.assertIn(" ".join(edge.evidence.split()), text(response))


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


class VerdictTests(SimpleTestCase):
    """Green only when every check on the way in is confirmed."""

    def connection(self, *layers, channel=None):
        from types import SimpleNamespace

        from ..request_channel import TAILNET_CHANNEL
        from ..request_connection import Connection

        return Connection(
            address="100.64.0.20",
            channel=channel or TAILNET_CHANNEL,
            device=None,
            serves=None,
            observer=None,
            identity=None,
            layers=tuple(SimpleNamespace(holds=held, conclusive=sure) for held, sure in layers),
        )

    def verdict(self, *layers):
        return self.connection(*layers).verdict

    def test_the_panels_tally_names_the_checks_and_their_whole(self):
        # Beside it the path counts steps; a bare "1 failed" reads as one of those.
        for layers in (((False, True), (True, False), (True, True)), ((True, False), (True, True), (True, True))):
            with self.subTest(layers):
                self.assertTrue(self.connection(*layers).tally.startswith("3 checks, "))

    def test_every_check_held_on_evidence_is_confirmed(self):
        self.assertEqual(self.verdict((True, True), (True, True)), "confirmed")

    def test_one_unconfirmed_check_is_not_green(self):
        self.assertEqual(self.verdict((True, True), (True, False)), "unconfirmed")

    def test_one_failed_check_is_failing(self):
        self.assertEqual(self.verdict((False, True), (True, False)), "failing")

    def test_the_dot_takes_the_tailnets_tone_only_once_every_check_is_confirmed(self):
        for layers, tone in (
            (((True, True),), "good"),
            (((True, False),), "neutral"),
            (((False, True),), "attention"),
        ):
            with self.subTest(layers):
                self.assertEqual(self.connection(*layers).tone, tone)

    def test_any_other_network_keeps_its_own_tone_whatever_the_checks_say(self):
        from ..request_channel import ELSEWHERE_CHANNEL, LOOPBACK_CHANNEL, NETWORK_CHANNEL, OPAQUE_CHANNEL

        for channel, tone in (
            (NETWORK_CHANNEL, "attention"),
            (LOOPBACK_CHANNEL, "attention"),
            (OPAQUE_CHANNEL, "serious"),
            (ELSEWHERE_CHANNEL, "serious"),
        ):
            for layers in (((True, True),), ((True, False),), ((False, True),)):
                with self.subTest(channel.id, layers=layers):
                    self.assertEqual(self.connection(*layers, channel=channel).tone, tone)
