"""A repository that cannot become a project is said in the log; the rest still do."""

from unittest import mock

from django.test import TestCase

from hq.platform.application import projects
from hq.platform.application.github_estate import Repository
from hq.platform.application.record_errors import RecordConflict
from hq.platform.application.security import cli_principal


class RefusedRepositoryTests(TestCase):
    def test_a_repository_that_cannot_be_a_project_is_logged_and_the_rest_are_taken_on(self):
        found = {
            name: Repository(name=name, record={}, observed_at=None) for name in ("example/refused", "example/taken")
        }
        save_project = projects.save_project

        def refusing(command, **kwargs):
            if command.repository_url.endswith("/example/refused"):
                raise RecordConflict("A project already holds that slug.")
            return save_project(command, **kwargs)

        with (
            mock.patch.object(projects, "repositories", return_value=found),
            mock.patch.object(projects, "save_project", refusing),
            self.assertLogs("severino.projects", "WARNING") as logged,
        ):
            result = projects.sync_repositories(principal=cli_principal())

        self.assertEqual(result["created"], ["taken"])
        (said,) = logged.output
        self.assertIn("example/refused", said)
        self.assertIn("A project already holds that slug.", said)
