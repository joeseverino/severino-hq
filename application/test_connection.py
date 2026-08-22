"""What HQ can prove about the request in front of it.

The value of this panel is entirely in whether a check can come back false. A
page that says "secure" no matter what is decoration, and decoration in a
security surface is worse than nothing: it is read as a measurement. So most of
what follows takes a fact away and asserts the page notices.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ProviderInventory

from .connection import channel_of, connection, headers_of, hops_of

A_TAILNET_ADDRESS = "100.64.0.5"
A_LAN_ADDRESS = "10.0.0.50"


def a_tailnet(*devices):
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={"records": list(devices), "observed_at": timezone.now()},
    )


def a_device(name, address, *, user="", tags=(), observer=False, reach=(), **extra):
    return {
        "name": name,
        "addresses": [address],
        "user": user,
        "tags": list(tags),
        "self": observer,
        "reach": [
            {"port": port, "who": list(who), "rules": [{"who": list(who), "line": 1}]}
            for port, who in reach
        ],
        **extra,
    }


def a_request(address=A_TAILNET_ADDRESS, *, host="hq.example.test", secure=True):
    request = RequestFactory().get(
        "/connection/", secure=secure, HTTP_HOST=host, REMOTE_ADDR=address
    )
    request.user = get_user_model()(username="someone", email="someone@example.test")
    return request


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class ChannelTests(TestCase):
    """The one part that runs on every page, so it may not touch anything."""

    def test_a_tailscale_address_is_the_tailnet(self):
        self.assertEqual(channel_of(A_TAILNET_ADDRESS).id, "tailnet")

    def test_a_private_address_is_the_local_network(self):
        self.assertEqual(channel_of(A_LAN_ADDRESS).id, "network")

    def test_a_public_address_is_not_recognised(self):
        self.assertEqual(channel_of("203.0.113.7").id, "elsewhere")

    def test_nonsense_is_not_quietly_treated_as_private(self):
        """The safe direction to be wrong in."""

        self.assertFalse(channel_of("not-an-address").private)

    def test_deciding_the_channel_costs_no_queries(self):
        with self.assertNumQueries(0):
            channel_of(A_TAILNET_ADDRESS)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class LayerTests(TestCase):
    def setUp(self):
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
            AUTHENTICATION_BACKENDS=["core.oidc.HQOIDCAuthenticationBackend"],
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
        ):
            found = connection(a_request())

        self.assertTrue(found.holds, [layer.id for layer in found.failing])

    def test_a_request_off_the_tailnet_says_so(self):
        with override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True):
            found = connection(a_request(A_LAN_ADDRESS))

        self.assertFalse(self.layer(found, "channel").holds)

    def test_the_gate_reports_when_it_is_not_being_enforced(self):
        """The check that would otherwise be a sentence rather than a fact."""

        with override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=False):
            found = connection(a_request())

        self.assertFalse(self.layer(found, "gate").holds)
        self.assertIn("not being enforced", self.layer(found, "gate").detail)

    @override_settings(
        AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]
    )
    def test_an_installed_password_backend_is_reported(self):
        found = connection(a_request())

        self.assertFalse(self.layer(found, "sign-in").holds)

    @override_settings(SESSION_COOKIE_SECURE=False)
    def test_a_session_cookie_missing_a_flag_is_reported(self):
        found = connection(a_request())

        layer = self.layer(found, "session")
        self.assertFalse(layer.holds)
        self.assertIn("Secure=off", layer.evidence)

    def test_a_published_name_is_not_treated_as_a_defence(self):
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

    def test_a_device_nothing_swept_is_not_reported_as_a_known_node(self):
        found = connection(a_request("100.64.0.77"))

        self.assertFalse(self.layer(found, "device").holds)


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
        """The shape a stolen session would have."""

        self.assertFalse(connection(a_request(A_LAN_ADDRESS)).identity.corroborated)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class LinkTests(TestCase):
    def test_a_relayed_peer_is_not_described_as_direct(self):
        a_tailnet(
            a_device(
                "a-laptop", A_TAILNET_ADDRESS, user="someone@example.test",
                relay="ord", direct_endpoint="",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        self.assertIn("Relayed", connection(a_request()).path_label)

    def test_a_direct_peer_is_reported_with_its_endpoint(self):
        a_tailnet(
            a_device(
                "a-laptop", A_TAILNET_ADDRESS, direct_endpoint="203.0.113.9:41641",
                relay="ord",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        self.assertEqual(connection(a_request()).path_label, "Direct")

    def test_a_handshake_that_never_happened_is_not_reported_as_an_age(self):
        """Tailscale writes the zero time for never, which ages to two millennia."""

        a_tailnet(
            a_device(
                "a-laptop", A_TAILNET_ADDRESS,
                last_handshake="0001-01-01T00:00:00Z",
            ),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

        self.assertEqual(connection(a_request()).handshake, "—")


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class ConnectionPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="someone", email="someone@example.test", password="not-used-here"
        )
        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))

    def test_the_page_needs_a_session(self):
        self.assertEqual(self.client.get(reverse("connection")).status_code, 302)

    def test_the_panel_the_dialog_lifts_out_is_on_the_page(self):
        """The dialog finds the panel by this hook, so its absence is a break."""

        self.client.force_login(self.user)

        response = self.client.get(reverse("connection"))

        self.assertContains(response, "data-connection-panel")


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
            "/connection/", HTTP_HOST="hq.example.test", REMOTE_ADDR=peer,
            **({"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {}),
        )
        return hops_of(request)

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

        judged = next(hop for hop in found if hop.role == "judged")
        self.assertIn("Nothing here identifies", judged.detail)

    def test_a_forwarded_header_from_an_unknown_peer_is_not_believed(self):
        """Otherwise a caller picks the address HQ judges them by."""

        found = self.hops(peer="203.0.113.9", forwarded="100.64.0.5")

        self.assertEqual([hop.value for hop in found], ["203.0.113.9"])

    def test_the_chain_reads_in_the_order_the_hops_happened(self):
        found = self.hops(peer="10.0.0.9", forwarded="100.64.0.5")

        self.assertEqual(
            [hop.value for hop in found], ["100.64.0.5", "10.0.0.9"]
        )


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

    def test_headers_hq_acts_on_come_first(self):
        found = headers_of(
            RequestFactory().get(
                "/connection/", HTTP_HOST="hq.example.test",
                HTTP_X_REAL_IP="100.64.0.5", HTTP_X_FORWARDED_FOR="100.64.0.5",
            )
        )

        self.assertTrue(found[0].used)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class CostTests(TestCase):
    """What this costs the pages that merely carry the control.

    The badge is in the header of every page in HQ, so anything it costs is
    paid on every render. The panel behind it is the expensive part -- it reads
    two inventories and evaluates the access policy -- and it is fetched when
    somebody opens it and not before.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="someone", email="someone@example.test", password="not-used-here"
        )
        a_tailnet(
            a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
            a_device("hq-host", "100.64.0.9", observer=True),
        )

    def test_the_badge_costs_nothing(self):
        """Arithmetic on one address: no query, no settings read, no inventory."""

        from core.context_processors import connection as badge

        request = a_request()
        with self.assertNumQueries(0):
            badge(request)

    def test_the_panel_reads_each_inventory_once(self):
        """Not once per device, per address, per layer or per header."""

        self.client.force_login(self.user)

        with self.assertNumQueries(6):
            self.client.get(reverse("connection"))


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
            "/connection/", secure=True, HTTP_HOST="hq.example.test",
            REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="172.18.0.1",
        )
        request.user = get_user_model()(username="someone")
        return connection(request)

    def test_it_is_not_reported_as_the_local_network(self):
        self.assertNotEqual(self.found().channel.id, "network")

    def test_it_says_the_address_never_arrived(self):
        self.assertEqual(self.found().channel.id, "opaque")

    def test_the_gate_does_not_claim_to_hold_while_judging_a_proxy(self):
        gate = next(
            layer for layer in self.found().layers if layer.id == "gate"
        )

        self.assertFalse(gate.holds)
        self.assertIn("admitting the proxy", gate.detail)


@override_settings(ALLOWED_HOSTS=["hq.example.test", "testserver"])
class DeclinedHeaderTests(TestCase):
    """A header carrying the right answer that HQ refuses to believe.

    The dangerous reading of "ignored" is that nobody wired it up, which
    invites somebody to wire it up. Declining `X-Real-IP` is the safer of two
    choices and the page has to be able to say which it is.
    """

    def header(self, name, **extra):
        request = RequestFactory().get(
            "/connection/", HTTP_HOST="hq.example.test", **extra
        )
        return next(h for h in headers_of(request) if h.name == name)

    def test_a_single_asserted_address_is_declined_rather_than_unread(self):
        found = self.header("X-Real-Ip", HTTP_X_REAL_IP="100.64.0.5")

        self.assertEqual(found.state, "declined")
        self.assertIn("chain", found.declined)

    def test_a_second_source_for_the_scheme_is_declined(self):
        found = self.header("X-Forwarded-Scheme", HTTP_X_FORWARDED_SCHEME="https")

        self.assertEqual(found.state, "declined")

    def test_a_header_nothing_has_an_opinion_on_is_merely_ignored(self):
        found = self.header("Accept-Language", HTTP_ACCEPT_LANGUAGE="en")

        self.assertEqual(found.state, "ignored")

    def test_declined_headers_sort_above_ignored_ones(self):
        found = headers_of(
            RequestFactory().get(
                "/connection/", HTTP_HOST="hq.example.test",
                HTTP_ACCEPT_LANGUAGE="en", HTTP_X_REAL_IP="100.64.0.5",
            )
        )
        states = [h.state for h in found]

        self.assertLess(states.index("declined"), states.index("ignored"))
