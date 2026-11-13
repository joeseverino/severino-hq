"""What the map page offers for following a trace, and for leaving one."""

from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.tests.host_only import host_only
from hq.platform.application.tests.markup import elements, select, within


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
        self.assertEqual(context["topology_trace_label"], "Dependents")
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
        context = self.page(lens="everything").context

        self.assertEqual(context["topology_view"], "everything")
        self.assertIsNone(context["topology_trace"])
        self.assertIsNone(context["topology_trace_focus"])
        self.assertEqual(context["topology_trace_label"], "")
        for unasked in ("topology_edges", "trace_direction_links", "trace_depth_links"):
            self.assertEqual(context[unasked], ())
        self.assertEqual(asks(context["trace_reset_url"]), {"lens": "everything"})


class MapBarTests(TestCase):
    """The map's view and its count never say everything and a part at once:
    the view chosen is the one the count describes, and the one link to what
    is held back lands on the view that holds nothing back."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="operator", password="not-a-real-password")
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="internal-name",
            kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"},
        )

    def bar(self, url=None, **params):
        """The view the bar's select has chosen, the number it says is shown
        out of how many, its link to what is held back, and its Clear."""

        with host_only():
            response = self.client.get(url or reverse("control_plane:topology"), params)
        options = [option for option in within(response, "name", "lens") if option.tag == "option"]
        chosen = next((option for option in options if "selected" in option.attrs), options[0])
        (shown,) = select(response, "data-filter-bar-shown")
        (summary,) = select(response, "role", "status")
        held = [link.attrs["href"] for link in elements(response, "a") if summary in link.ancestors]
        clear = [link.attrs["href"] for link in elements(response, "a") if link.words == "Clear filters"]
        cards = len(select(response, "data-topology-node"))
        return {
            "view": chosen.attrs["value"],
            "shown": int(shown.words),
            "total": response.context["topology_total"],
            "cards": cards,
            "held": held,
            "clear": clear,
        }

    def test_the_main_lanes_name_what_they_hold_back_and_link_to_everything(self):
        main = self.bar()

        self.assertEqual(main["view"], "")
        self.assertEqual(main["shown"], main["cards"])
        self.assertLess(main["shown"], main["total"])
        self.assertEqual(main["clear"], [])
        (more,) = main["held"]
        everything = self.bar(more)
        self.assertEqual(everything["view"], "everything")
        self.assertEqual((everything["shown"], everything["cards"]), (main["total"], main["total"]))
        self.assertEqual(everything["held"], [])
        (back,) = everything["clear"]
        self.assertEqual(self.bar(back)["view"], "")

    def test_a_lens_holds_nothing_back(self):
        lens = self.bar(lens="unobserved-resources")

        self.assertEqual(lens["view"], "unobserved-resources")
        self.assertEqual(lens["shown"], lens["total"])
        self.assertEqual(lens["held"], [])
