from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from example_extension import demo


class PageTests(TestCase):
    def setUp(self) -> None:
        self.client.force_login(get_user_model().objects.create_user("operator"))
        demo.plant(None)

    def test_a_signed_in_operator_sees_the_page_in_the_host_layout(self) -> None:
        response = self.client.get(reverse("example_extension:index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "page.html")
        self.assertContains(response, reverse("example_extension:note", args=["a-week-in"]))

    def test_each_page_the_demo_names_is_a_note_it_wrote(self) -> None:
        for address in demo.pages():
            with self.subTest(address):
                self.assertEqual(self.client.get(address).status_code, 200)

    def test_a_note_the_demo_never_wrote_is_not_found(self) -> None:
        self.assertEqual(self.client.get(reverse("example_extension:note", args=["unwritten"])).status_code, 404)

    def test_a_visitor_is_sent_to_sign_in(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("example_extension:index"))
        self.assertEqual(response.status_code, 302)
