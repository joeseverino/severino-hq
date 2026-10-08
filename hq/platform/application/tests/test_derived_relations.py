"""Relations a page reads from the other end: a certificate's services, a
connection's problems and purpose, a backlog's own things, a list under its types."""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.control_plane.models import ProviderConnection

from ..attention import MOST_NAMED, assets
from ..connection_context import connections_context
from ..entity_links import entity_link
from ..resource_context import RecordStatus, record_list
from ..security import cli_principal
from ..services import certificate_use
from .test_relationships import connected, estate


def host_only():
    return mock.patch("hq.platform.application.domains.extension_domains", return_value=())


class NamedBacklogTests(TestCase):
    def test_a_backlog_card_names_its_first_things_as_links_and_counts_the_rest(self):
        for name in ("Desk", "Bench", "Camera", "Anvil"):
            Asset.objects.create(item_name=name)

        (item,) = assets()

        self.assertEqual(item.magnitude, 4)
        self.assertTrue(item.body.startswith("Anvil, Bench, Camera and 1 other. "))
        self.assertEqual([action.label for action in item.actions], ["Anvil", "Bench", "Camera"])
        self.assertEqual(len(item.actions), MOST_NAMED)
        self.assertTrue(all(action.url.startswith("/assets/") for action in item.actions))

    def test_a_backlog_of_one_names_it_and_counts_nothing(self):
        Asset.objects.create(item_name="Desk")

        (item,) = assets()

        self.assertTrue(item.body.startswith("Desk. "))


class CertificateUseTests(TestCase):
    def test_a_certificate_lists_the_services_served_with_it_and_its_unused_names(self):
        services = [SimpleNamespace(hostname=name) for name in ("b.example.com", "a.example.com", "c.example.net")]
        serving = {"a.example.com": ("wildcard",), "b.example.com": ("wildcard", "other"), "c.example.net": ("other",)}
        with (
            mock.patch("hq.platform.application.services.service_catalog", return_value=services),
            mock.patch("hq.platform.application.services.certificates_serving", side_effect=serving.get),
        ):
            use = certificate_use("wildcard", ("*.example.com", "example.org"))

        self.assertEqual(use.used_by, ("a.example.com", "b.example.com"))
        self.assertEqual(use.unused, ("example.org",))


class ConnectionRowsTests(TestCase):
    def setUp(self):
        estate(1)
        connected("example-broken", "cloudflare_api")
        ProviderConnection.objects.filter(connection_ref="example-broken").update(
            reachable=False
        )

    def context(self):
        with host_only(), mock.patch(
            "hq.platform.application.plugins.plugin_connection_specs", return_value=()
        ):
            return connections_context(principal=cli_principal())

    def test_every_connection_sits_under_the_part_of_hq_that_uses_it(self):
        found = self.context()

        self.assertTrue(all(row.used_for for row in found.rows))
        self.assertEqual(sum(len(rows) for _label, rows in found.purposes), len(found.rows))
        # The controller's connections lead, under the group their page sits in.
        self.assertEqual(found.purposes[0][0], "Infrastructure")

    def test_a_connection_with_an_open_problem_counts_it_and_links_to_it(self):
        found = self.context()

        row = next(row for row in found.rows if row.instance.connection_ref == "example-broken")
        about = entity_link("connection", row.instance.label).url
        self.assertGreaterEqual(row.problems, 1)
        self.assertEqual(row.as_dict()["open_problems"], row.problems)
        self.assertIn("about=", row.problems_url)
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))
        with host_only():
            queue = self.client.get(row.problems_url)
            page = self.client.get(reverse("control_plane:connections"))
        self.assertEqual(
            {item["subject"]["url"] for group in queue.context["action_groups"] for item in group["items"]},
            {about},
        )
        self.assertContains(page, f'href="{row.problems_url.replace("&", "&amp;")}"')
        self.assertContains(page, 'class="row-group"')


def _row(key, kind, label, tone="healthy", **extra):
    return SimpleNamespace(
        key=key, kind=kind, kind_label=label, shown_name=key, summary=extra.get("summary", ""),
        record_status=RecordStatus("working" if tone == "healthy" else "degraded", tone, "Working" if tone == "healthy" else "Has a problem"),
    )


class RecordListTests(TestCase):
    ROWS = [
        _row("b-proxy", "npm.proxy_host", "Proxy host"),
        _row("a-dns", "adguard.rewrite", "Internal DNS record", summary="192.0.2.10"),
        _row("c-dns", "adguard.rewrite", "Internal DNS record", tone="degraded"),
    ]

    def test_rows_sit_under_their_type_with_what_needs_a_look_first(self):
        listed = record_list(self.ROWS)

        self.assertEqual([(group.label, len(group.rows), group.unsettled) for group in listed.groups],
                         [("Internal DNS record", 2, 1), ("Proxy host", 1, 0)])
        self.assertEqual([row.key for row in listed.groups[0].rows], ["c-dns", "a-dns"])
        self.assertEqual((listed.total, listed.unsettled, listed.filtered), (3, 1, False))

    def test_the_filter_keeps_rows_by_their_words_and_by_one_type(self):
        by_words = record_list(self.ROWS, query="192.0.2")
        by_type = record_list(self.ROWS, kind="npm.proxy_host")
        unknown_type = record_list(self.ROWS, kind="example.none")

        self.assertEqual([row.key for group in by_words.groups for row in group.rows], ["a-dns"])
        self.assertEqual([group.label for group in by_type.groups], ["Proxy host"])
        # Every type is still offered while one is chosen.
        self.assertEqual(len(by_type.types), 2)
        self.assertEqual((by_type.shown, by_type.total), (1, 3))
        self.assertFalse(unknown_type.filtered)

    def test_the_page_draws_a_group_for_each_type_and_filters_by_the_url(self):
        estate(2)
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "pw"))

        page = self.client.get(reverse("control_plane:list"))
        one = self.client.get(reverse("control_plane:list"), {"q": "s0.example.com"})

        self.assertContains(page, 'class="row-group"')
        self.assertGreater(page.context["records"].total, one.context["records"].shown)
        self.assertEqual(one.context["records"].shown, 1)
