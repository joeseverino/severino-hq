"""A navigation entry is called what its page is titled."""

from django.test import SimpleTestCase

from hq.platform.application.domains import domain_navigation, page_title


class NavigationNameTests(SimpleTestCase):
    def test_an_entry_takes_its_page_s_title_and_names_itself_only_for_an_untitled_page(self):
        from hq.domains.projects.views import PostureView

        labels = {item.route: item.label for item in domain_navigation()}

        self.assertEqual(labels["posture"], PostureView.page_title)
        self.assertEqual(page_title("dashboard"), "")
        self.assertEqual(labels["dashboard"], "Dashboard")
