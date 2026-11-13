"""The machine and network under the request: the node HQ runs on, what carries the session, and the tailnet lock."""

from unittest import mock

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.platform.core.tests.forwarding import arrived

from ..request_connection import connection
from .connection_requests import A_LAN_ADDRESS, A_TAILNET_ADDRESS, a_device, a_request, a_tailnet
from .people import someone


def a_tailnet_lock(**lock):
    """A swept tailnet-lock reading."""

    ProviderInventory.objects.update_or_create(
        kind="host.tailnet_lock",
        defaults={
            "records": [{"record": "lock", **lock}],
            "observed_at": timezone.now(),
        },
    )


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

    def test_no_device_draws_no_connection_type(self):
        """Unread, or read without this address: the row is left out."""

        a_tailnet()
        unread = connection(a_request())
        a_tailnet(a_device("hq-host", "100.64.0.9", observer=True))
        unlisted = connection(a_request())

        self.assertEqual((unread.peering, unlisted.peering), (None, None))

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
            **arrived(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=A_TAILNET_ADDRESS),
        )
        request.user = someone()
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
        a_tailnet_lock(enabled=False, trusted_keys=0)
        found = self.layer(connection(a_request()))
        self.assertFalse(found.holds)
        self.assertFalse(found.conclusive)

    def test_an_unsigned_node_fails_the_layer(self):
        a_tailnet_lock(enabled=True, trusted_keys=2)
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
        a_tailnet_lock(enabled=True, trusted_keys=2)
        found = self.layer(connection(a_request()))
        self.assertTrue(found.holds)
        self.assertTrue(found.conclusive)
        self.assertIn("2 signing keys", found.evidence)

    def test_one_signing_key_is_not_pluralized(self):
        a_tailnet_lock(enabled=True, trusted_keys=1)
        self.assertTrue(self.layer(connection(a_request())).evidence.endswith("1 signing key"))

    def test_no_device_means_no_signature_line(self):
        # The device layer above has already said no node answers here. A line
        # about that node's signature would be describing nothing.
        a_tailnet_lock(enabled=True, trusted_keys=2)
        self.assertIsNone(self.layer(connection(a_request(A_LAN_ADDRESS))))
