from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class PageTests(TestCase):
    def test_a_signed_in_operator_sees_the_page_in_the_host_layout(self) -> None:
        self.client.force_login(get_user_model().objects.create_user("operator"))
        response = self.client.get(reverse("example_extension:index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "page.html")
        self.assertContains(response, "example.two")

    def test_a_visitor_is_sent_to_sign_in(self) -> None:
        response = self.client.get(reverse("example_extension:index"))
        self.assertEqual(response.status_code, 302)
