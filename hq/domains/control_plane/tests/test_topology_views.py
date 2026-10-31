"""What the map page offers for following a trace, and for leaving one."""

from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.tests.host_only import host_only


def asks(url: str) -> dict[str, str]:
    """What a link's query asks for, one value per name."""

    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


class TraceLinkTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="operator", password="not-a-real-password")
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="internal-name",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

    def page(self, **params):
        with host_only():
            return self.client.get(reverse("control_plane:topology"), params)

    def test_a_trace_s_links_keep_its_focus_and_its_view(self):
        context = self.page(lens="unobserved-resources", focus="resource:internal-name", direction="inbound").context

        self.assertEqual(context["topology_trace_focus"].id, "resource:internal-name")
        self.assertEqual(context["topology_trace_label"], "What depends on this")
        self.assertFalse(context["topology_everything"])
        directions = [asks(link["url"]) for link in context["trace_direction_links"]]
        depths = [asks(link["url"]) for link in context["trace_depth_links"]]
        for asked in (*directions, *depths):
            self.assertEqual((asked["focus"], asked["lens"]), ("resource:internal-name", "unobserved-resources"))
        self.assertEqual({asked["direction"] for asked in directions}, {"inbound", "outbound", "both"})
        self.assertEqual({asked["direction"] for asked in depths}, {"inbound"})
        self.assertEqual(
            [asked["depth"] for asked in depths], [str(link["depth"]) for link in context["trace_depth_links"]]
        )
        self.assertEqual(asks(context["trace_reset_url"]), {"lens": "unobserved-resources"})

    def test_the_whole_map_asked_for_offers_no_trace_and_resets_to_itself(self):
        context = self.page(all=1).context

        self.assertTrue(context["topology_everything"])
        self.assertIsNone(context["topology_trace"])
        self.assertIsNone(context["topology_trace_focus"])
        self.assertEqual(context["topology_trace_label"], "")
        for unasked in ("topology_edges", "trace_direction_links", "trace_depth_links"):
            self.assertEqual(context[unasked], ())
        self.assertEqual(urlsplit(context["trace_reset_url"]).query, "")
