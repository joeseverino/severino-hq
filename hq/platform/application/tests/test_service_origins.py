"""Where a service is served from: how its origin is resolved, where that came from, and how it is worded."""

from django.test import TestCase
from django.urls import reverse
from django.utils.html import strip_tags

from hq.domains.control_plane.models import ManagedResource

from ..services import find_service
from .service_world import declare_world, healthy


class OriginResolutionTests(TestCase):
    def setUp(self):
        declare_world(containers=True)

    def _proxy(self, forward_host="10.0.0.10", forward_port=8000):
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": forward_host,
                    "forward_port": forward_port,
                },
            )
        )
        return find_service("app.example.com")

    def test_a_forwarding_address_resolves_to_a_host_and_its_container(self):
        origin = self._proxy().origin

        self.assertTrue(origin.known)
        self.assertEqual(origin.host, "app-host")
        self.assertEqual(origin.container, "web")

    def test_a_port_no_container_claims_names_the_host_and_stops(self):
        """A host HQ can name; a container it cannot. Silence beats a guess,
        and ``800`` is not the container listening on ``8000``."""
        for port in (9999, 800):
            with self.subTest(port=port):
                ManagedResource.objects.filter(key="app-proxy").delete()
                origin = self._proxy(forward_port=port).origin

                self.assertEqual((origin.host, origin.container), ("app-host", ""))

    def test_an_address_no_host_claims_is_reported_as_unknown(self):
        service = self._proxy(forward_host="10.9.9.9")

        self.assertFalse(service.origin.known)
        self.assertIn("matches no known machine", " ".join(service.faults))

    def _rewrite(self, answer="10.0.0.10"):
        healthy(
            ManagedResource.objects.create(
                key="app-rewrite",
                kind="adguard.rewrite",
                spec={"domain": "app.example.com", "answer": answer},
            )
        )

    def test_a_record_with_no_ingress_names_where_the_name_is_sent(self):
        """A rewrite pointing at a machine is a statement of where it is served.

        Nothing proxies this name, so the rewrite is what says where it goes.
        """

        self._rewrite()

        origin = find_service("app.example.com").origin

        self.assertEqual(origin.host, "app-host")
        self.assertFalse(origin.external)

    def test_an_ingress_outranks_the_record_that_points_at_it(self):
        """Both name an origin, and they mean different things by it. The
        record says where the name goes, which for a proxied name is the proxy.
        The proxy says where the request is finally served. Filled in whichever
        order the rows come back, the record would describe proxied names as
        being served by the proxy box itself.
        """

        self._rewrite(answer="10.0.0.9")
        origin = self._proxy().origin

        self.assertEqual(origin.address, "10.0.0.10:8000")
        self.assertEqual(origin.container, "web")


class OriginProvenanceTests(TestCase):
    """A page must not describe an ingress a name does not have."""

    def setUp(self):
        declare_world(containers=True)

    def _page(self, hostname="app.example.com"):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        user = get_user_model().objects.create_user(username="op", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(user)
        return self.client.get(reverse("control_plane:service", kwargs={"hostname": hostname}))

    def test_a_name_with_only_a_record_is_not_told_an_ingress_forwards_it(self):
        """A record-derived origin is not described as an ingress.

        A record-derived origin rendered as "ingress forwards to 10.0.0.10"
        directly beneath an Ingress card reading "Not declared": one page
        contradicting itself in two adjacent elements.
        """

        healthy(
            ManagedResource.objects.create(
                key="app-rewrite",
                kind="adguard.rewrite",
                spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            )
        )

        response = self._page()

        # Where it runs is a row of the path's parts, not a sentence under the cards.
        self.assertNotContains(response, "Ingress forwards to")
        self.assertContains(response, reverse("control_plane:machine", args=["app-host"]))

    def test_a_proxied_name_still_says_its_ingress_forwards(self):
        healthy(
            ManagedResource.objects.create(
                key="app-proxy",
                kind="npm.proxy_host",
                spec={
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 6379,
                },
            )
        )

        response = self._page()

        self.assertIn("10.0.0.10:6379", strip_tags(response.content.decode()))


class OriginWordingTests(TestCase):
    """One fact, one phrasing, whichever surface asks for it. The board and the
    name's own page read the same Origin, so they must not say "unknown host"
    on one and "Served by Cloudflare Pages" on the other.
    """

    def test_something_outside_is_named_rather_than_called_unknown(self):
        from ..whereabouts import Origin

        origin = Origin(address="example.pages.dev")

        self.assertTrue(origin.external)
        self.assertEqual(origin.qualifier, "")
        self.assertNotEqual(origin.headline, "unknown host")

    def test_a_known_machine_reads_as_itself(self):
        from ..whereabouts import Origin

        origin = Origin(address="10.0.0.9:8000", host="a-docker-host", container="probe")

        self.assertEqual(origin.headline, "a-docker-host\xa0· probe")
        self.assertEqual(origin.qualifier, "")

    def test_an_address_nothing_claims_still_says_so(self):
        """The caveat has to survive: an ingress pointing somewhere HQ cannot
        describe is worth interrupting for."""

        from ..whereabouts import Origin

        origin = Origin(address="10.9.9.9:8080")

        self.assertEqual(origin.qualifier, "unknown host")


class ConnectedMachineTests(TestCase):
    """A machine HQ holds a credential for is not an unknown host.

    The proxy in front of the cPanel site read as "unknown host" while the
    connections page listed that exact address under a name.
    """

    def test_an_address_a_connection_points_at_is_named(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderConnection

        from ..whereabouts import locate

        ProviderConnection.objects.create(
            connection_ref="a-shared-host",
            controller_id="a-controller",
            provider="ssh",
            endpoint="203.0.113.10:21098",
            reaches=[],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        origin = locate("203.0.113.10:443", ())

        self.assertEqual(origin.host, "a-shared-host")
        self.assertTrue(origin.known)
        self.assertEqual(origin.qualifier, "")

    def test_an_address_no_credential_points_at_is_still_unknown(self):
        from ..whereabouts import locate

        origin = locate("10.9.9.9:443", ())

        self.assertEqual(origin.qualifier, "unknown host")

    def test_a_url_endpoint_matches_by_its_hostname(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderConnection

        from ..whereabouts import locate

        ProviderConnection.objects.create(
            connection_ref="a-proxy",
            controller_id="a-controller",
            provider="npm",
            endpoint="https://proxy.example",
            reaches=[],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        origin = locate("proxy.example:81", ())

        self.assertEqual(origin.host, "a-proxy")


class PortlessOriginTests(TestCase):
    """An address with no port is all host. `rpartition` puts the whole string
    in its last element when the separator is absent, so a naive split matches
    a DNS answer naming a known machine against an empty host.
    """

    def test_a_bare_address_matches_the_machine_it_names(self):
        from ..whereabouts import locate

        origin = locate(
            "10.0.0.9",
            ({"name": "a-docker-host", "addresses": ["10.0.0.9"]},),
        )

        self.assertEqual(origin.host, "a-docker-host")
        self.assertFalse(origin.external)

    def test_a_bare_address_matches_a_connection_too(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderConnection

        from ..whereabouts import locate

        ProviderConnection.objects.create(
            connection_ref="a-shared-host",
            controller_id="a-controller",
            provider="ssh",
            endpoint="203.0.113.10:21098",
            reaches=[],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        origin = locate("203.0.113.10", ())

        self.assertEqual(origin.headline, "a-shared-host")

    def test_a_name_nothing_claims_is_still_answered_elsewhere(self):
        """The heuristic it guards must survive: a portless address nothing
        knows is a name answered outside this network."""

        from ..whereabouts import locate

        origin = locate("example.pages.dev", ())

        self.assertTrue(origin.external)


class LoopbackOriginTests(TestCase):
    """A proxy forwarding to itself still names a machine.

    Terminating TLS and forwarding over loopback is the safer arrangement
    (the request never crosses a network between the proxy and the thing it
    serves) and it made every such service unresolvable: no machine is
    declared at 127.0.0.1 because every machine is, so matching by address
    reported "unknown host" for the one hop that never left the box.
    """

    def a_container_on(self, host, name, port):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        from ..services import CONTAINER_KIND

        ProviderInventory.objects.update_or_create(
            kind=CONTAINER_KIND,
            defaults={
                "records": [{"name": name, "host": host, "ports": [port], "state": "running"}],
                "observed_at": timezone.now(),
            },
        )

    def test_loopback_resolves_to_the_machine_listening_on_that_port(self):
        from ..whereabouts import locate

        self.a_container_on("a-docker-host", "an-app", 8000)

        origin = locate("127.0.0.1:8000", ({"name": "a-docker-host"},))

        self.assertEqual(origin.host, "a-docker-host")
        self.assertEqual(origin.container, "an-app")

    def test_a_port_nothing_or_two_machines_listen_on_names_no_machine(self):
        """Better silent than confidently wrong about which box it meant."""
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        from ..services import CONTAINER_KIND
        from ..whereabouts import locate

        ProviderInventory.objects.update_or_create(
            kind=CONTAINER_KIND,
            defaults={
                "records": [
                    {"name": "one", "host": "host-a", "ports": [8000], "state": "running"},
                    {"name": "two", "host": "host-b", "ports": [8000], "state": "running"},
                ],
                "observed_at": timezone.now(),
            },
        )

        machines = ({"name": "host-a"}, {"name": "host-b"})
        self.assertEqual(locate("127.0.0.1:9999", machines).host, "")
        self.assertEqual(locate("127.0.0.1:8000", machines).host, "")
