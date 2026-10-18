"""A saved copy of HQ never states an example connection as the reader's own."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

NOTICE = "This is an example connection, not yours."
MARK = '<span class="example-connection">example connection</span>'


class ExampleConnectionTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("reader"))

    def pages(self):
        return {
            "dashboard": self.client.get(reverse("dashboard")),
            "connection": self.client.get(reverse("connection")),
        }

    @override_settings(SEVERINO_RENDERED_COPY=True)
    def test_a_saved_copy_says_so_wherever_it_describes_the_reader(self):
        pages = self.pages()

        # The connection panel, as a page and as the dialog's copy.
        self.assertContains(pages["connection"], NOTICE, count=1)
        # The dashboard's connection reading; the menu's line links to the panel.
        self.assertContains(pages["dashboard"], MARK, count=1)

    def test_a_live_answer_carries_no_mark(self):
        for name, response in self.pages().items():
            with self.subTest(page=name):
                self.assertNotContains(response, NOTICE)
                self.assertNotContains(response, "example-connection")
