"""An empty list says why it is empty."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.capabilities import capability_words
from hq.platform.application.tests.markup import links


def offers(page, label: str, href: str) -> bool:
    """Whether the page links ``href`` under words that start with ``label``."""

    return any(words.startswith(label) and target == href for words, target in links(page))


class EmptyListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("operator", "operator@example.com", "unused-password")

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_list_with_nothing_in_it_says_so_in_its_own_words(self):
        response = self.client.get(reverse("assets:list"))

        self.assertContains(response, "No assets yet.")
        self.assertTrue(offers(response, capability_words("asset.create"), reverse("assets:create")))
        self.assertNotContains(response, "Nothing matches.")

    def test_a_list_emptied_by_its_search_offers_the_way_back(self):
        response = self.client.get(reverse("assets:list"), {"q": "no such asset"})

        self.assertContains(response, "Nothing matches.")
        self.assertTrue(offers(response, "Clear filters", "?"))
        self.assertNotContains(response, "No assets yet.")

    def test_an_empty_table_page_says_what_is_missing_in_place_of_its_table(self):
        """A page whose table region holds no row draws the region's empty
        state in its own words and no headings over nothing."""

        for name, says in (
            ("control_plane:list", "No records yet."),
            ("control_plane:machines", "No machines yet."),
            ("control_plane:services", "No services."),
        ):
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertContains(response, says)
                self.assertNotContains(response, "<th")
                self.assertNotContains(response, "Nothing matches.")

    def test_a_table_emptied_by_its_filter_offers_the_way_back(self):
        response = self.client.get(reverse("control_plane:list"), {"q": "no such record"})

        self.assertContains(response, "Nothing matches.")
        self.assertNotContains(response, "No records yet.")
        self.assertNotContains(response, "<th")
        self.assertTrue(offers(response, "Clear filters", reverse("control_plane:list")))
