"""An empty list says why it is empty."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class EmptyListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            "operator", "operator@example.com", "unused-password"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_list_with_nothing_in_it_says_so_in_its_own_words(self):
        response = self.client.get(reverse("assets:list"))

        self.assertContains(response, "No assets yet.")
        self.assertNotContains(response, "Nothing matches.")

    def test_a_list_emptied_by_its_search_offers_the_way_back(self):
        response = self.client.get(reverse("assets:list"), {"q": "no such asset"})

        self.assertContains(response, "Nothing matches.")
        self.assertContains(response, '<a href="?">Clear the search and filters</a>', html=True)
        self.assertNotContains(response, "No assets yet.")
