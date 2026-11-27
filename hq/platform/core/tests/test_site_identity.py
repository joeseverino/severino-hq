"""What HQ can work out about its own deployment, it does."""

from django.test import TestCase, override_settings

from hq.domains.content.content_sync import index_project
from hq.domains.projects.models import Project


@override_settings(
    CONTENT_INDEX_URL="https://www.example.com/content-index.json",
    CONTENT_INDEX_PROJECT_SLUG="",
)
class IndexProjectTests(TestCase):
    def test_the_project_serving_the_index_owns_it(self):
        Project.objects.create(name="Other", slug="other", public_url="https://other.example.com/")
        site = Project.objects.create(name="Site", slug="site", public_url="https://www.example.com/")
        self.assertEqual(index_project(), site)

    def test_two_claimants_is_no_answer(self):
        Project.objects.create(name="A", slug="a", public_url="https://www.example.com/")
        Project.objects.create(name="B", slug="b", public_url="https://www.example.com/blog/")
        self.assertIsNone(index_project())
