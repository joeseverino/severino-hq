"""Every repository the GitHub App reads becomes a project when a sweep records it."""

from django.test import TestCase

from hq.domains.control_plane.observations.github import REPOSITORY_KIND
from hq.domains.projects.models import Project

from ..report_testing import bridged


def sweep(*records: dict) -> dict:
    return bridged(
        "inventory",
        {
            REPOSITORY_KIND: {
                "ok": True,
                "records": [
                    {"connection_ref": "example-github", "url": f"https://github.com/{r['repository']}", **r}
                    for r in records
                ],
            }
        },
        controller_id="controller",
    )


class RepositorySyncTests(TestCase):
    def test_a_second_sweep_changes_nothing(self):
        Project.objects.create(name="Beta", slug="beta")
        sweep({"repository": "example/alpha"}, {"repository": "example/beta"})
        before = {project.slug: project.updated_at for project in Project.objects.all()}

        sweep({"repository": "example/alpha"}, {"repository": "example/beta"})

        self.assertEqual({project.slug: project.updated_at for project in Project.objects.all()}, before)
