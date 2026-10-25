"""A record's slug is made once, from its name, and never collides."""

from django.test import TestCase

from hq.domains.projects.models import Project

from ..models import free_slug
from .estate_sizes import assert_cost_does_not_grow


class FreeSlugTests(TestCase):
    def test_a_fresh_name_is_its_own_slug(self):
        self.assertEqual(Project.objects.create(name="Home Lab").slug, "home-lab")

    def test_a_taken_slug_gets_the_next_free_number(self):
        for _ in range(3):
            Project.objects.create(name="Home Lab")

        self.assertEqual(
            sorted(Project.objects.values_list("slug", flat=True)), ["home-lab", "home-lab-2", "home-lab-3"]
        )

    def test_a_longer_name_sharing_the_prefix_is_not_a_collision(self):
        Project.objects.create(name="Home Lab Notes")

        self.assertEqual(Project.objects.create(name="Home Lab").slug, "home-lab")

    def test_a_name_with_nothing_to_slugify_uses_the_fallback(self):
        self.assertEqual(Project.objects.create(name="!!!").slug, "project")

    def test_a_long_name_leaves_room_for_its_number_within_the_column(self):
        limit = Project._meta.get_field("slug").max_length
        first = Project.objects.create(name="a" * 400)
        second = Project.objects.create(name="a" * 400)

        self.assertLessEqual(len(second.slug), limit)
        self.assertEqual(second.slug, f"{first.slug}-2")

    def test_resaving_a_record_keeps_its_slug(self):
        project = Project.objects.create(name="Home Lab")
        project.name = "Renamed"
        project.save()

        self.assertEqual(project.slug, "home-lab")

    def test_finding_a_free_slug_costs_the_same_however_crowded(self):
        def arrange(count):
            for _ in range(Project.objects.count(), count):
                Project.objects.create(name="Home Lab")

        def taken(slug):
            """The projects the slug steps past: home-lab-6 is free after five."""

            return int(slug.removeprefix("home-lab-")) - 1

        assert_cost_does_not_grow(
            self,
            sizes=(1, 5),
            arrange=arrange,
            draw=lambda _count: free_slug(Project(name="Home Lab"), "Home Lab", "project"),
            rows=taken,
        )
