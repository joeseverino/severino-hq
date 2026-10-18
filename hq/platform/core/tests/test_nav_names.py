"""A navigation entry is called what its page is called."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.domains import domain_navigation
from hq.platform.application.tests.host_only import host_only
from hq.platform.core.bench import seed

# Entries whose page is one of several things the entry lists, so the page is
# titled by the thing: a domain's page is named for the domain.
NAMED_FOR_ITS_SUBJECT = frozenset({"zones:index"})


class NavigationNameTests(TestCase):
    def test_every_entry_is_named_as_its_page_is_titled(self):
        seeded = seed(0.05)
        self.client.force_login(seeded.user)
        differing = {}
        with host_only():
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
