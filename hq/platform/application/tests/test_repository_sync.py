"""Every repository the GitHub App reads becomes a project when a sweep records it."""

from django.test import TestCase

from hq.domains.control_plane.observations.github import REPOSITORY_KIND
from hq.domains.projects.models import Project

from ..security import cli_principal
from ..sweep import record_sweep


def sweep(*records: dict) -> dict:
    return record_sweep(
        {
            REPOSITORY_KIND: {
                "ok": True,
                "records": [
                    {"connection_ref": "example-github", "url": f"https://github.com/{r['repository']}", **r}
                    for r in records
                ],
            }
        },
        principal=cli_principal(),
        controller_id="controller",
    )


def by_url() -> dict[str, Project]:
    return {project.repository_url: project for project in Project.objects.exclude(repository_url="")}


class RepositorySyncTests(TestCase):
    def test_a_repository_no_project_names_becomes_one(self):
        sweep({"repository": "example/alpha"}, {"repository": "example/hidden", "private": True})

        found = by_url()
        alpha = found["https://github.com/example/alpha"]
        self.assertEqual((alpha.name, alpha.slug, alpha.status), ("alpha", "alpha", Project.Status.ACTIVE))
        self.assertIn("https://github.com/example/hidden", found)

    def test_an_archived_repository_is_an_archived_project(self):
        sweep({"repository": "example/old", "archived": True})

        self.assertEqual(by_url()["https://github.com/example/old"].status, Project.Status.ARCHIVED)

    def test_a_project_that_names_the_repository_is_left_as_it_is(self):
        project = Project.objects.create(
            name="Alpha tool",
            slug="alpha-tool",
            status=Project.Status.PAUSED,
            repository_url="https://github.com/Example/Alpha",
            notes="Kept by hand.",
        )
        stamp = project.updated_at

        sweep({"repository": "example/alpha"})

        project.refresh_from_db()
        self.assertEqual(
            (project.name, project.status, project.notes, project.updated_at),
            ("Alpha tool", Project.Status.PAUSED, "Kept by hand.", stamp),
        )
        self.assertEqual(Project.objects.count(), 1)

    def test_a_project_with_no_repository_and_the_repositorys_name_takes_its_url(self):
        project = Project.objects.create(name="Beta", slug="beta", description="Written by hand.")

        sweep({"repository": "example/Beta"})

        project.refresh_from_db()
        self.assertEqual(project.repository_url, "https://github.com/example/Beta")
        self.assertEqual(project.description, "Written by hand.")
        self.assertEqual(Project.objects.count(), 1)

    def test_a_taken_slug_gets_a_free_one(self):
        Project.objects.create(name="Gamma", slug="gamma", repository_url="https://example.com/gamma")

        sweep({"repository": "example/gamma"})

        self.assertEqual(by_url()["https://github.com/example/gamma"].slug, "gamma-2")

    def test_a_second_sweep_changes_nothing(self):
        Project.objects.create(name="Beta", slug="beta")
        sweep({"repository": "example/alpha"}, {"repository": "example/beta"})
        before = {project.slug: project.updated_at for project in Project.objects.all()}

        sweep({"repository": "example/alpha"}, {"repository": "example/beta"})

        self.assertEqual({project.slug: project.updated_at for project in Project.objects.all()}, before)

    def test_a_repository_that_leaves_the_reading_keeps_its_project(self):
        sweep({"repository": "example/alpha"})

        sweep({"repository": "example/beta"})

        self.assertIn("https://github.com/example/alpha", by_url())
