"""What HQ can work out about its own deployment, it does."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings

from config.settings import site_host
from content.content_sync import index_project
from projects.models import Project


class SiteHostTests(SimpleTestCase):
    def test_the_first_trusted_origin_names_it(self):
        self.assertEqual(
            site_host(["https://hq.example.com"], ["other.example.com"]), "hq.example.com"
        )

    def test_otherwise_the_first_concrete_allowed_host(self):
        self.assertEqual(
            site_host([], [".example.com", "*", "hq.example.com"]), "hq.example.com"
        )

    def test_nothing_known_is_localhost(self):
        self.assertEqual(site_host([], []), "localhost")


@override_settings(
    CONTENT_INDEX_URL="https://www.example.com/content-index.json",
    CONTENT_INDEX_PROJECT_SLUG="",
)
class IndexProjectTests(TestCase):
    def test_the_project_serving_the_index_owns_it(self):
        Project.objects.create(name="Other", slug="other", public_url="https://other.example.com/")
        site = Project.objects.create(
            name="Site", slug="site", public_url="https://www.example.com/"
        )
        self.assertEqual(index_project(), site)

    def test_two_claimants_is_no_answer(self):
        Project.objects.create(name="A", slug="a", public_url="https://www.example.com/")
        Project.objects.create(name="B", slug="b", public_url="https://www.example.com/blog/")
        self.assertIsNone(index_project())

    @override_settings(CONTENT_INDEX_PROJECT_SLUG="b")
    def test_a_named_project_wins(self):
        Project.objects.create(name="A", slug="a", public_url="https://www.example.com/")
        named = Project.objects.create(name="B", slug="b")
        self.assertEqual(index_project(), named)
