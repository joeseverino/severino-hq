"""The hops a request came through: which one HQ believes, the headers it read, and the ones it refuses."""

from unittest import mock

from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from hq.platform.core.tests.estate_sizes import assert_subject_cost_does_not_grow
from hq.platform.core.tests.forwarding import arrived

from ..request_addresses import addresses_of, addresses_of_hq
from ..request_connection import connection
from ..request_headers import headers_of
from ..request_path import address_chain
from .connection_requests import A_TAILNET_ADDRESS, a_device, a_request, a_tailnet
from .host_only import host_only
from .people import someone


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
            **arrived(REMOTE_ADDR=peer, **({"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {})),
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
        # HQ falls back to the first proxy's address, as the forwarded-header
        # middleware does, and says that is what it did.
        self.assertEqual(judged.value, "172.18.0.1")
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

    def test_a_body_s_type_and_length_are_headers_the_request_carried(self):
        request = RequestFactory().post("/connection/", {"a": "b"}, HTTP_HOST="hq.example.test")

        found = {header.name: header.value for header in headers_of(request)}

        self.assertEqual(found["Content-Type"], request.META["CONTENT_TYPE"])
        self.assertEqual(found["Content-Length"], request.META["CONTENT_LENGTH"])

    def test_a_type_and_length_the_server_set_empty_are_not_listed(self):
        found = self.headers(CONTENT_TYPE="", CONTENT_LENGTH="")

        self.assertNotIn("Content-Length", found)
        self.assertNotIn("Content-Type", found)


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
        self.user = someone()
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

    def test_the_panel_costs_the_same_however_many_devices_there_are(self):
        """Not once per device, per address, per layer or per header."""

        def arrange(count):
            a_tailnet(
                a_device("a-laptop", A_TAILNET_ADDRESS, user="someone@example.test"),
                a_device("hq-host", "100.64.0.9", observer=True),
                *(a_device(f"another-{index}", f"100.64.1.{index}") for index in range(count)),
            )

        self.client.force_login(self.user)
        # The panel names the requester's device and HQ's, never the others.
        assert_subject_cost_does_not_grow(
            self, sizes=(1, 12), arrange=arrange, draw=lambda _count: self.client.get(reverse("connection"))
        )


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
            **arrived(REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="172.18.0.1"),
        )
        request.user = someone()
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
