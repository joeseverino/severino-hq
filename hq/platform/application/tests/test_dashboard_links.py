"""The dashboard's links: every one it offers is a link a reader can follow."""

from django.template.loader import render_to_string
from django.test import TestCase, override_settings

from ..outward_links import offered_links
from .markup import elements

OPERATOR_LINKS = '[{"label": "Status", "sub": "status page", "href": "https://status.example.com/"}]'


@override_settings(SEVERINO_DASHBOARD_LINKS=OPERATOR_LINKS)
class DashboardLinkTests(TestCase):
    def test_every_offered_link_is_drawn_as_one(self):
        offered = offered_links()
        page = render_to_string("dashboard.html#links", {"external_links": offered})
        drawn = {anchor.attrs.get("href"): anchor for anchor in elements(page, "a")}

        self.assertTrue(offered)
        for link in offered:
            with self.subTest(link=link["label"]):
                self.assertIn(link["href"], drawn, "an offered link is drawn as a link")

    def test_hq_s_own_page_opens_in_place_and_the_rest_in_a_new_tab(self):
        offered = offered_links()
        page = render_to_string("dashboard.html#links", {"external_links": offered})
        drawn = {anchor.attrs.get("href"): anchor for anchor in elements(page, "a")}
        own = [link["href"] for link in offered if link["href"].startswith("/")]

        self.assertTrue(own, "HQ offers a page of its own")
        for link in offered:
            with self.subTest(link=link["label"]):
                anchor = drawn[link["href"]]
                self.assertEqual(anchor.attrs.get("target") == "_blank", link["href"] not in own)
