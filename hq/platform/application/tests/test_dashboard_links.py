"""The dashboard's links: every one it offers is a link a reader can follow."""

from django.template.loader import render_to_string
from django.test import TestCase, override_settings

from hq.platform.application.markup_testing import elements

from ..outward_links import offered_links

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
