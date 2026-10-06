"""A navigation entry is called what its page is called."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.domains import domain_navigation
from hq.platform.core.bench import seed

# Entries whose page is one of several things the entry lists, so the page is
# titled by the thing: a domain's page is named for the domain.
NAMED_FOR_ITS_SUBJECT = frozenset({"zones:index"})


class NavigationNameTests(TestCase):
    def test_every_entry_is_named_as_its_page_is_titled(self):
        seeded = seed(0.05)
        self.client.force_login(seeded.user)
        differing = {}
        with (
            mock.patch("hq.platform.application.domains.extension_domains", return_value=()),
            mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()),
        ):
            for item in domain_navigation():
                if item.route in NAMED_FOR_ITS_SUBJECT:
                    continue
                response = self.client.get(reverse(item.route), follow=True)
                page = response.context.get("page") if response.context else None
                # A page that draws its own head (the dashboard, the API
                # reference) has no title to hold to.
                if page is not None and page.title != item.label:
                    differing[item.route] = (item.label, page.title)

        self.assertEqual(differing, {})
