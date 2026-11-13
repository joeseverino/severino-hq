"""What the internet reaches, derived from the service paths HQ already walks."""

from django.test import TestCase

from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow, distinct

from ..exposure import GATED, OPEN, PRIVATE, UNKNOWN, UNROUTED, exposure_of_name, status_at
from ..projection import projection_scope
from .markup import select, text, with_class, within
from .test_paths import CLOUDFLARE, PUBLIC_RANGE, estate, record, store


@PUBLIC_RANGE
class ExposureTests(TestCase):
    def setUp(self):
        estate()

    def level(self, hostname):
        with projection_scope():
            return exposure_of_name(hostname).level

    def test_a_public_name_through_the_edge_is_open(self):
        self.assertEqual(self.level("shop.example.com"), OPEN)

    def test_a_name_only_a_rewrite_answers_on_the_tailnet_is_private(self):
        self.assertEqual(self.level("app.example.com"), PRIVATE)

    def test_a_public_record_answering_with_a_tailnet_address_is_private(self):
        store("cloudflare.dns_record", record("vpn.example.com", "A", "100.64.0.10", proxied=False))

        self.assertEqual(self.level("vpn.example.com"), PRIVATE)

    def test_a_reading_that_restricts_the_name_gates_it(self):
        store(
            "cloudflare.access_app",
            {
                "connection_ref": CLOUDFLARE,
                "id": "a1",
                "name": "Shop admin",
                "domain": "shop.example.com",
                "type": "self_hosted",
            },
        )

        with projection_scope():
            exposure = exposure_of_name("shop.example.com")

        self.assertEqual(exposure.level, GATED)
        self.assertEqual(exposure.worst.gates, ("Behind Access: Shop admin",))
        self.assertIn("behind a login as shop.example.com", exposure.sentence)

    def test_a_gate_on_every_path_gates_the_name(self):
        store(
            "cloudflare.access_app",
            {
                "connection_ref": CLOUDFLARE,
                "id": "a1",
                "name": "Shop",
                "domain": "shop.example.com/*",
                "type": "self_hosted",
            },
        )

        self.assertEqual(self.level("shop.example.com"), GATED)

    def test_a_gate_on_one_path_leaves_the_name_open_and_says_where(self):
        store(
            "cloudflare.access_app",
            {
                "connection_ref": CLOUDFLARE,
                "id": "a1",
                "name": "Shop admin",
                "domain": "shop.example.com/admin*",
                "type": "self_hosted",
            },
        )

        with projection_scope():
            exposure = exposure_of_name("shop.example.com")

        self.assertEqual(exposure.level, OPEN)
        self.assertEqual(exposure.worst.gates, ())
        self.assertEqual(exposure.worst.path_gates, ("/admin*",))
        self.assertIn("login only at /admin*", exposure.sentence)

    def test_a_private_name_says_the_widest_network_any_route_admits(self):
        store(
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
            {"domain": "app.example.com", "answer": "10.0.0.5", "connection_ref": "example-adguard"},
        )

        with projection_scope():
            exposure = exposure_of_name("app.example.com")

        self.assertEqual((exposure.level, exposure.label), (PRIVATE, "Home network and tailnet"))

    def test_a_name_nothing_resolves_is_unrouted(self):
        self.assertEqual(self.level("nothing.example.com"), UNROUTED)


class RankingTests(TestCase):
    def test_exposure_only_ever_lowers_urgency(self):
        self.assertEqual(
            [status_at("serious", level) for level in (OPEN, UNKNOWN, GATED, PRIVATE, UNROUTED)],
            ["serious", "serious", "attention", "attention", "neutral"],
        )
        self.assertEqual(
            [status_at("attention", level) for level in (OPEN, GATED, PRIVATE, UNROUTED)],
            ["attention", "attention", "attention", "neutral"],
        )
        self.assertEqual(status_at("good", OPEN), "good")


@PUBLIC_RANGE
class ServiceReachTests(TestCase):
    """Who reaches each name, on the pages that already list the names."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        estate()
        self.user = get_user_model().objects.create_user("example-operator", password="x")
        self.client.force_login(self.user)

    def page(self):
        from django.urls import reverse

        return self.client.get(reverse("control_plane:services"))

    def service(self, name):
        from django.urls import reverse

        return self.client.get(reverse("control_plane:service", args=[name])).content.decode()

    def test_each_name_says_who_can_reach_it(self):
        (said,) = select(self.page(), "title", "Open to the internet as example.com")

        self.assertEqual(said.words, "Internet")

    def test_a_private_name_offers_no_gate(self):
        body = self.service("app.example.com")

        self.assertEqual([item.words for item in select(body, "data-exposure", "private")], ["Tailnet only"])
        self.assertNotIn("Add an access list", body)

    def test_the_header_the_path_and_the_list_name_one_audience(self):
        """Each reach a name can have, said by the page's header, its path and
        the services list in the words ``reach`` declares for it."""

        from hq.domains.control_plane.models import ManagedResource

        from ..reach import LOCAL_NETWORK, PUBLIC, TAILNET_ONLY

        rewrites = (
            {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
            {"domain": "home.example.com", "answer": "10.0.0.5", "connection_ref": "example-adguard"},
        )
        store("adguard.rewrite", *rewrites)
        # Declared, so the services list holds a row for each.
        for spec in rewrites:
            ManagedResource.objects.create(key=spec["domain"].split(".")[0], kind="adguard.rewrite", spec=spec)
        ManagedResource.objects.create(
            key="shop",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["shop.example.com"],
                "forward_scheme": "http",
                "forward_host": "198.51.100.20",
                "forward_port": 8080,
                "connection_ref": "example-npm",
            },
        )
        listed = self.page().content.decode()
        for hostname, reach in (
            ("shop.example.com", PUBLIC),
            ("home.example.com", LOCAL_NETWORK),
            ("app.example.com", TAILNET_ONLY),
        ):
            with self.subTest(reach=reach.id):
                body = self.service(hostname)
                (header,) = (item for item in within(body, "class", "page-title-row") if item.attrs.get("title"))
                (path,) = select(body, "data-exposure")
                (row,) = (item for item in select(listed, "data-exposure") if hostname in item.attrs["title"])

                steps = [item.words for item in with_class(body, "hop-label")]

                self.assertEqual((header.words, header.attrs["title"]), (reach.label, reach.detail))
                # The network the path crosses is called what the audience calls it.
                self.assertTrue(any(step and reach.label.startswith(step) for step in steps), steps)
                for said in (path.words, row.words):
                    self.assertIn(reach.label.lower(), said.lower())

    def test_an_open_name_behind_a_declared_proxy_offers_its_access_list(self):
        from hq.domains.control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="example-shop-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["shop.example.com"],
                "forward_scheme": "http",
                "forward_host": "198.51.100.20",
                "forward_port": 8080,
                "connection_ref": "example-npm",
            },
        )

        body = self.service("shop.example.com")

        self.assertIn("Add an access list", text(body))
        self.assertIn("infrastructure.resource.update/?target=example-shop-proxy", body)

    def test_it_asks_to_sign_in(self):
        from django.urls import reverse

        self.client.logout()

        self.assertEqual(self.page().status_code, 302)
        self.assertIn(reverse("login"), self.page()["Location"])

    def test_more_names_cost_no_more_queries(self):
        def arrange(count):
            store(
                "cloudflare.dns_record",
                *(record(f"extra-{index}.example.com", "A", "198.51.100.20") for index in range(count)),
            )

        assert_cost_does_not_grow(
            self,
            sizes=(1, 8),
            arrange=arrange,
            draw=lambda _count: self.page(),
            rows=distinct(r"\bextra-\d+\b"),
            slack=2,
        )
