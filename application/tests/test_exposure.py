"""What the internet reaches, derived from the service paths HQ already walks."""

from __future__ import annotations

from django.test import TestCase

from ..exposure import GATED, OPEN, PRIVATE, UNROUTED, exposure_of_name, status_at
from ..projection import projection_scope
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
        store("cloudflare.access_app", {"connection_ref": CLOUDFLARE, "id": "a1", "name": "Shop admin",
                                        "domain": "shop.example.com", "type": "self_hosted"})

        with projection_scope():
            exposure = exposure_of_name("shop.example.com")

        self.assertEqual(exposure.level, GATED)
        self.assertEqual(exposure.worst.gates, ("Behind Access: Shop admin",))
        self.assertIn("behind a gate as shop.example.com", exposure.sentence)

    def test_a_name_nothing_resolves_is_unrouted(self):
        self.assertEqual(self.level("nothing.example.com"), UNROUTED)


class RankingTests(TestCase):
    def test_exposure_only_ever_lowers_urgency(self):
        self.assertEqual(
            [status_at("serious", level) for level in (OPEN, GATED, PRIVATE, UNROUTED)],
            ["serious", "attention", "attention", "neutral"],
        )
        self.assertEqual(
            [status_at("attention", level) for level in (OPEN, GATED, PRIVATE, UNROUTED)],
            ["attention", "attention", "attention", "neutral"],
        )
        self.assertEqual(status_at("good", OPEN), "good")


@PUBLIC_RANGE
class ExposurePageTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        estate()
        self.user = get_user_model().objects.create_user("example-operator", password="x")
        self.client.force_login(self.user)

    def page(self):
        from django.urls import reverse

        return self.client.get(reverse("control_plane:exposure"))

    def test_names_are_listed_worst_first_with_who_can_reach_them(self):
        body = self.page().content.decode()

        self.assertLess(body.index("shop.example.com"), body.index("app.example.com"))
        self.assertIn("Open to the internet", body)
        self.assertIn("Private networks only", body)

    def test_an_open_name_behind_a_declared_proxy_offers_its_access_list(self):
        from control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="example-shop-proxy", kind="npm.proxy_host",
            spec={"domain_names": ["shop.example.com"], "forward_scheme": "http",
                  "forward_host": "198.51.100.20", "forward_port": 8080,
                  "connection_ref": "example-npm"},
        )

        body = self.page().content.decode()

        self.assertIn("Put an access list in front of example-shop-proxy", body)
        self.assertIn("infrastructure.resource.update/?target=example-shop-proxy", body)

    def test_it_asks_to_sign_in(self):
        from django.urls import reverse

        self.client.logout()

        self.assertEqual(self.page().status_code, 302)
        self.assertIn(reverse("login"), self.page()["Location"])

    def test_more_names_cost_no_more_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as few:
            self.page()
        store("cloudflare.dns_record", *(
            record(f"extra-{index}.example.com", "A", "198.51.100.20") for index in range(8)
        ))
        with CaptureQueriesContext(connection) as many:
            self.page()

        self.assertLessEqual(len(many), len(few) + 2)
